import asyncio
from types import TracebackType
from collections.abc import Callable
from typing import Self

import pytest
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

import app.cache.redis as redis_module
from app.cache.keys import (
    CHART_PERIOD,
    CHART_TTL_SECONDS,
    MARKET_LIST_KEY,
    MARKET_LIST_LOCK_KEY,
    MARKET_LIST_LOCK_TTL_SECONDS,
    MARKET_LIST_TTL_SECONDS,
    PRICE_TTL_SECONDS,
    STALE_CHART_TTL_SECONDS,
    STALE_PRICE_TTL_SECONDS,
    TICKER_REFRESH_LOCK_KEY,
    TICKER_REFRESH_LOCK_TTL_SECONDS,
    chart_key,
    price_key,
    stale_chart_key,
    stale_price_key,
)
from app.cache.redis import (
    COMPARE_AND_DELETE_SCRIPT,
    CacheSource,
    CacheWrite,
    RedisCache,
    RedisUnavailableError,
    create_redis_cache,
)
from app.core.config import Settings


class FakePipeline:
    def __init__(self, client: "FakeRedis") -> None:
        self.client = client
        self.writes: list[tuple[str, str, int]] = []

    def set(self, name: str, value: str, *, ex: int) -> Self:
        self.writes.append((name, value, ex))
        return self

    async def execute(self) -> list[object]:
        self.client.commands.append(("pipeline_execute", tuple(self.writes)))
        failure = self.client.failures.get("pipeline_execute")
        if failure is not None:
            raise failure
        for key, value, ttl_seconds in self.writes:
            self.client.values[key] = value
            self.client.ttls[key] = ttl_seconds
        return [True] * len(self.writes)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.commands: list[tuple[object, ...]] = []
        self.failures: dict[str, BaseException] = {}
        self.closed = False

    def _raise_failure(self, operation: str) -> None:
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure

    async def get(self, name: str) -> str | None:
        self.commands.append(("get", name))
        self._raise_failure("get")
        return self.values.get(name)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.commands.append(("mget", tuple(keys)))
        self._raise_failure("mget")
        return [self.values.get(key) for key in keys]

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool | None:
        self.commands.append(("set", name, value, ex, nx))
        self._raise_failure("set")
        if nx and name in self.values:
            return None
        self.values[name] = value
        self.ttls[name] = ex
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        self.commands.append(("pipeline", transaction))
        self._raise_failure("pipeline")
        return FakePipeline(self)

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str,
    ) -> int:
        self.commands.append(("eval", script, numkeys, *keys_and_args))
        self._raise_failure("eval")
        key, expected_value = keys_and_args
        if self.values.get(key) != expected_value:
            return 0
        del self.values[key]
        self.ttls.pop(key, None)
        return 1

    async def aclose(self) -> None:
        self.commands.append(("aclose",))
        self._raise_failure("aclose")
        self.closed = True


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def cache(fake_redis: FakeRedis) -> RedisCache:
    return RedisCache(fake_redis)


def test_key_builders_and_ttls_match_contract() -> None:
    assert MARKET_LIST_KEY == "market:list"
    assert price_key("KRW-BTC") == "price:KRW-BTC"
    assert stale_price_key("KRW-BTC") == "stale:price:KRW-BTC"
    assert chart_key("KRW-BTC") == "chart:KRW-BTC:1d"
    assert stale_chart_key("KRW-BTC") == "stale:chart:KRW-BTC:1d"
    assert CHART_PERIOD == "1d"
    assert ":1m" not in chart_key("KRW-BTC")

    assert MARKET_LIST_TTL_SECONDS == 86_400
    assert PRICE_TTL_SECONDS == 5
    assert STALE_PRICE_TTL_SECONDS == 3_600
    assert CHART_TTL_SECONDS == 300
    assert STALE_CHART_TTL_SECONDS == 86_400
    assert MARKET_LIST_LOCK_KEY == "lock:market:list"
    assert MARKET_LIST_LOCK_TTL_SECONDS == 10
    assert TICKER_REFRESH_LOCK_KEY == "lock:ticker:refresh"
    assert TICKER_REFRESH_LOCK_TTL_SECONDS == 5


@pytest.mark.parametrize(
    "builder",
    [price_key, stale_price_key, chart_key, stale_chart_key],
)
def test_key_builders_reject_empty_market_codes(
    builder: Callable[[str], str],
) -> None:
    with pytest.raises(ValueError):
        builder("  ")


@pytest.mark.asyncio
async def test_factory_uses_settings_and_does_not_send_network_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis()
    calls: list[tuple[str, dict[str, object]]] = []

    class RedisStub:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> FakeRedis:
            calls.append((url, kwargs))
            return fake

    monkeypatch.setattr(redis_module, "Redis", RedisStub)
    settings = Settings(
        _env_file=None,
        redis_url="redis://cache.internal:6380/3",
        redis_timeout_seconds=1.25,
    )

    created = create_redis_cache(settings)

    assert isinstance(created, RedisCache)
    assert calls == [
        (
            "redis://cache.internal:6380/3",
            {
                "encoding": "utf-8",
                "decode_responses": True,
                "socket_connect_timeout": 1.25,
                "socket_timeout": 1.25,
            },
        )
    ]
    assert fake.commands == []


@pytest.mark.asyncio
async def test_json_miss_malformed_payload_and_cached_null_are_distinct_by_source(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    fake_redis.values["broken"] = "not-json"
    fake_redis.values["stale"] = "null"

    assert await cache.get_json("missing") is None
    assert await cache.get_json("broken") is None
    lookup = await cache.get_fresh_or_stale(
        fresh_key="missing",
        stale_key="stale",
    )

    assert lookup.value is None
    assert lookup.source is CacheSource.STALE
    assert lookup.is_stale is True


@pytest.mark.asyncio
async def test_set_and_get_json_preserve_unicode_and_ttl(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    value = {"marketCode": "KRW-BTC", "koreanName": "비트코인"}

    await cache.set_json("coin", value, ttl_seconds=24)

    assert fake_redis.values["coin"] == (
        '{"marketCode":"KRW-BTC","koreanName":"비트코인"}'
    )
    assert fake_redis.ttls["coin"] == 24
    assert await cache.get_json("coin") == value


@pytest.mark.asyncio
async def test_mget_preserves_order_and_empty_input_sends_no_command(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    fake_redis.values.update({"first": "1", "third": "3"})

    assert await cache.get_many_json([]) == []
    assert fake_redis.commands == []
    assert await cache.get_many_json(["third", "missing", "first"]) == [3, None, 1]
    assert fake_redis.commands == [("mget", ("third", "missing", "first"))]


@pytest.mark.asyncio
async def test_batch_and_fresh_stale_writes_use_one_transaction_pipeline(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    await cache.set_many_json(
        [CacheWrite("one", {"value": 1}, 5), CacheWrite("two", [2], 60)]
    )
    await cache.set_fresh_and_stale(
        fresh_key="fresh",
        fresh_ttl_seconds=5,
        stale_key="stale",
        stale_ttl_seconds=3600,
        value={"price": 100},
    )

    pipeline_commands = [
        command for command in fake_redis.commands if command[0] == "pipeline"
    ]
    execute_commands = [
        command for command in fake_redis.commands if command[0] == "pipeline_execute"
    ]
    assert pipeline_commands == [("pipeline", True), ("pipeline", True)]
    assert len(execute_commands) == 2
    assert fake_redis.ttls == {"one": 5, "two": 60, "fresh": 5, "stale": 3600}


@pytest.mark.asyncio
async def test_fresh_then_stale_then_miss_lookup_order(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    fake_redis.values.update({"fresh": '"new"', "stale": '"old"'})
    fresh = await cache.get_fresh_or_stale(fresh_key="fresh", stale_key="stale")
    del fake_redis.values["fresh"]
    stale = await cache.get_fresh_or_stale(fresh_key="fresh", stale_key="stale")
    del fake_redis.values["stale"]
    miss = await cache.get_fresh_or_stale(fresh_key="fresh", stale_key="stale")

    assert (fresh.value, fresh.source) == ("new", CacheSource.FRESH)
    assert (stale.value, stale.source) == ("old", CacheSource.STALE)
    assert (miss.value, miss.source) == (None, CacheSource.MISS)


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("get", ConnectionError("private redis host")),
        ("set", TimeoutError("private redis timeout")),
        ("mget", ResponseError("private protocol response")),
        ("pipeline_execute", ConnectionError("private pipeline host")),
        ("eval", ResponseError("private eval body")),
        ("aclose", ConnectionError("private close host")),
    ],
)
@pytest.mark.asyncio
async def test_redis_failures_become_sanitized_internal_error(
    operation: str,
    error: Exception,
    fake_redis: FakeRedis,
) -> None:
    fake_redis.failures[operation] = error
    cache = RedisCache(fake_redis)

    with pytest.raises(RedisUnavailableError) as exc_info:
        if operation == "get":
            await cache.get_json("key")
        elif operation == "set":
            await cache.set_json("key", {}, ttl_seconds=1)
        elif operation == "mget":
            await cache.get_many_json(["key"])
        elif operation == "pipeline_execute":
            await cache.set_many_json([CacheWrite("key", {}, 1)])
        elif operation == "eval":
            await cache.compare_and_delete(key="lock", expected_value="token")
        else:
            await cache.aclose()

    assert "private" not in str(exc_info.value)
    assert exc_info.value.operation
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_compare_and_delete_uses_atomic_lua_only(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    fake_redis.values["lock:key"] = "owner-token"

    assert (
        await cache.compare_and_delete(key="lock:key", expected_value="other-token")
        is False
    )
    assert fake_redis.values["lock:key"] == "owner-token"
    assert (
        await cache.compare_and_delete(key="lock:key", expected_value="owner-token")
        is True
    )
    assert "lock:key" not in fake_redis.values
    assert [command[0] for command in fake_redis.commands] == ["eval", "eval"]
    assert all(
        command[1] == COMPARE_AND_DELETE_SCRIPT for command in fake_redis.commands
    )


@pytest.mark.asyncio
async def test_empty_inputs_and_invalid_ttls_do_not_send_commands(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    await cache.set_many_json([])
    with pytest.raises(ValueError):
        await cache.set_json("key", {}, ttl_seconds=0)
    with pytest.raises(ValueError):
        await cache.set_if_absent(key="lock", value="token", ttl_seconds=-1)

    assert fake_redis.commands == []


@pytest.mark.asyncio
async def test_json_serialization_error_is_not_redis_unavailable(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    with pytest.raises(TypeError):
        await cache.set_json("key", object(), ttl_seconds=5)

    assert fake_redis.commands == []


@pytest.mark.asyncio
async def test_redis_command_cancellation_propagates_unchanged(
    cache: RedisCache,
    fake_redis: FakeRedis,
) -> None:
    fake_redis.failures["get"] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cache.get_json("key")
