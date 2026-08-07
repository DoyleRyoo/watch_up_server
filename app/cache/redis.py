"""공유 비동기 Redis 연결 위에 JSON cache와 lock 원시 연산을 제공한다."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings


DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class RedisUnavailableError(Exception):
    """Redis 명령 실패를 외부 상세 없이 service 계층에 알리는 내부 오류."""

    def __init__(self, operation: str) -> None:
        super().__init__(f"Redis operation failed: {operation}")
        self.operation = operation


class CacheSource(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISS = "miss"


@dataclass(frozen=True, slots=True)
class CacheLookup:
    value: object | None
    source: CacheSource

    @property
    def is_stale(self) -> bool:
        return self.source is CacheSource.STALE


@dataclass(frozen=True, slots=True)
class CacheWrite:
    key: str
    value: object
    ttl_seconds: int


class AsyncPipeline(Protocol):
    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
    ) -> Self: ...

    async def execute(self) -> list[object]: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class AsyncRedis(Protocol):
    async def get(self, name: str) -> bytes | str | None: ...

    async def mget(self, keys: Sequence[str]) -> list[bytes | str | None]: ...

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool | str | bytes | None: ...

    def pipeline(self, transaction: bool = True) -> AsyncPipeline: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str,
    ) -> object: ...

    async def aclose(self) -> None: ...


class RedisCache:
    """하나의 공유 Redis pool에서 JSON cache와 lock 원시 연산을 수행한다.

    이 계층은 장애 정책을 정하지 않고 `RedisUnavailableError`만 전달한다. cache miss,
    stale fallback, Upbit 직접 호출 여부는 각 business service가 결정한다.
    """

    def __init__(self, client: AsyncRedis) -> None:
        self._client = client

    async def get_json(self, key: str) -> object | None:
        _validate_key(key)
        raw = await self._get_raw(key)
        hit, value = _decode_json(raw)
        return value if hit else None

    async def get_many_json(self, keys: Sequence[str]) -> list[object | None]:
        active_keys = list(keys)
        if not active_keys:
            return []
        for key in active_keys:
            _validate_key(key)

        raw_values = await self._mget_raw(active_keys)
        return [
            value if hit else None
            for hit, value in (_decode_json(raw) for raw in raw_values)
        ]

    async def set_json(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: int,
    ) -> None:
        _validate_key(key)
        _validate_ttl(ttl_seconds)
        payload = _encode_json(value)
        try:
            await self._client.set(key, payload, ex=ttl_seconds)
        except RedisError:
            raise RedisUnavailableError("set") from None

    async def set_many_json(self, writes: Sequence[CacheWrite]) -> None:
        active_writes = list(writes)
        if not active_writes:
            return

        encoded_writes: list[tuple[CacheWrite, str]] = []
        for write in active_writes:
            _validate_key(write.key)
            _validate_ttl(write.ttl_seconds)
            encoded_writes.append((write, _encode_json(write.value)))

        try:
            async with self._client.pipeline(transaction=True) as pipeline:
                for write, payload in encoded_writes:
                    pipeline.set(write.key, payload, ex=write.ttl_seconds)
                await pipeline.execute()
        except RedisError:
            raise RedisUnavailableError("pipeline set") from None

    async def set_fresh_and_stale(
        self,
        *,
        fresh_key: str,
        fresh_ttl_seconds: int,
        stale_key: str,
        stale_ttl_seconds: int,
        value: object,
    ) -> None:
        await self.set_many_json(
            [
                CacheWrite(fresh_key, value, fresh_ttl_seconds),
                CacheWrite(stale_key, value, stale_ttl_seconds),
            ]
        )

    async def get_fresh_or_stale(
        self,
        *,
        fresh_key: str,
        stale_key: str,
    ) -> CacheLookup:
        _validate_key(fresh_key)
        _validate_key(stale_key)
        fresh_raw, stale_raw = await self._mget_raw([fresh_key, stale_key])

        fresh_hit, fresh_value = _decode_json(fresh_raw)
        if fresh_hit:
            return CacheLookup(fresh_value, CacheSource.FRESH)

        stale_hit, stale_value = _decode_json(stale_raw)
        if stale_hit:
            return CacheLookup(stale_value, CacheSource.STALE)

        return CacheLookup(None, CacheSource.MISS)

    async def set_if_absent(
        self,
        *,
        key: str,
        value: str,
        ttl_seconds: int,
    ) -> bool:
        _validate_key(key)
        _validate_ttl(ttl_seconds)
        if not value:
            raise ValueError("lock value must not be empty")
        try:
            result = await self._client.set(
                key,
                value,
                ex=ttl_seconds,
                nx=True,
            )
        except RedisError:
            raise RedisUnavailableError("lock acquire") from None
        return bool(result)

    async def compare_and_delete(self, *, key: str, expected_value: str) -> bool:
        _validate_key(key)
        if not expected_value:
            raise ValueError("expected lock value must not be empty")
        try:
            result = await self._client.eval(
                COMPARE_AND_DELETE_SCRIPT,
                1,
                key,
                expected_value,
            )
        except RedisError:
            raise RedisUnavailableError("lock release") from None
        return bool(result)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except RedisError:
            raise RedisUnavailableError("close") from None

    async def _get_raw(self, key: str) -> bytes | str | None:
        try:
            return await self._client.get(key)
        except RedisError:
            raise RedisUnavailableError("get") from None

    async def _mget_raw(self, keys: Sequence[str]) -> list[bytes | str | None]:
        try:
            return await self._client.mget(keys)
        except RedisError:
            raise RedisUnavailableError("mget") from None


RedisCacheFactory = Callable[[Settings], RedisCache]


def create_redis_cache(settings: Settings) -> RedisCache:
    """Redis 명령을 보내지 않고 지연 연결되는 비동기 pool을 만든다."""

    redis_url = settings.redis_url.strip() or DEFAULT_REDIS_URL
    client = Redis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=settings.redis_timeout_seconds,
        socket_timeout=settings.redis_timeout_seconds,
    )
    return RedisCache(cast(AsyncRedis, client))


def _encode_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _decode_json(raw: bytes | str | None) -> tuple[bool, object | None]:
    if raw is None:
        return False, None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False, None
    try:
        return True, cast(object | None, json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        # 손상되거나 이전 schema인 값은 성공 데이터로 신뢰하지 않고 cache miss로 다룬다.
        return False, None


def _validate_key(key: str) -> None:
    if not key:
        raise ValueError("Redis key must not be empty")


def _validate_ttl(ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        raise ValueError("Redis TTL must be positive")


COMPARE_AND_DELETE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
""".strip()


__all__ = [
    "COMPARE_AND_DELETE_SCRIPT",
    "CacheLookup",
    "CacheSource",
    "CacheWrite",
    "RedisCache",
    "RedisCacheFactory",
    "RedisUnavailableError",
    "create_redis_cache",
]
