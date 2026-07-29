"""Token-owned Redis locks and bounded cache recheck behavior."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar
from uuid import uuid4

from app.cache.keys import (
    LOCK_CACHE_RECHECK_ATTEMPTS,
    LOCK_CACHE_RECHECK_INTERVAL_SECONDS,
)
from app.cache.redis import RedisCache, RedisUnavailableError
from app.core.errors import AppError, ErrorCode


logger = logging.getLogger("uvicorn.error")
T = TypeVar("T")

Sleeper = Callable[[float], Awaitable[None]]
TokenFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class LockLease:
    key: str
    token: str


class RedisLockManager:
    def __init__(
        self,
        cache: RedisCache,
        *,
        token_factory: TokenFactory | None = None,
    ) -> None:
        self._cache = cache
        self._token_factory = token_factory or (lambda: uuid4().hex)

    async def acquire(self, *, key: str, ttl_seconds: int) -> LockLease | None:
        token = self._token_factory()
        if not token:
            raise ValueError("lock token must not be empty")
        acquired = await self._cache.set_if_absent(
            key=key,
            value=token,
            ttl_seconds=ttl_seconds,
        )
        return LockLease(key, token) if acquired else None

    async def release(self, lease: LockLease) -> bool:
        return await self._cache.compare_and_delete(
            key=lease.key,
            expected_value=lease.token,
        )

    @asynccontextmanager
    async def lock(
        self,
        *,
        key: str,
        ttl_seconds: int,
    ) -> AsyncIterator[LockLease | None]:
        lease = await self.acquire(key=key, ttl_seconds=ttl_seconds)
        try:
            yield lease
        finally:
            if lease is not None:
                try:
                    await self.release(lease)
                except RedisUnavailableError:
                    logger.warning("Failed to release Redis lock key=%s", lease.key)


async def wait_for_cache_refresh(
    read_cache: Callable[[], Awaitable[T | None]],
    *,
    sleeper: Sleeper = asyncio.sleep,
) -> T:
    """Wait at most 500ms for another lock owner to populate the cache."""

    for _ in range(LOCK_CACHE_RECHECK_ATTEMPTS):
        await sleeper(LOCK_CACHE_RECHECK_INTERVAL_SECONDS)
        cached = await read_cache()
        if cached is not None:
            return cached

    raise AppError(
        code=ErrorCode.CACHE_REFRESH_IN_PROGRESS,
        message="캐시를 갱신하고 있습니다. 잠시 후 다시 시도해주세요.",
    )


__all__ = [
    "LockLease",
    "RedisLockManager",
    "wait_for_cache_refresh",
]
