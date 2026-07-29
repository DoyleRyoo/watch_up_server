import asyncio
import logging

import pytest
from redis.exceptions import ConnectionError

from app.cache.lock import RedisLockManager, wait_for_cache_refresh
from app.cache.redis import RedisCache
from app.core.errors import AppError, ErrorCode
from tests.test_redis_cache import FakeRedis


@pytest.mark.asyncio
async def test_lock_acquire_uses_unique_token_set_nx_ex_and_allows_one_owner() -> None:
    fake = FakeRedis()
    tokens = iter(["owner-one", "owner-two"])
    manager = RedisLockManager(RedisCache(fake), token_factory=lambda: next(tokens))

    first, second = await asyncio.gather(
        manager.acquire(key="lock:ticker:refresh", ttl_seconds=5),
        manager.acquire(key="lock:ticker:refresh", ttl_seconds=5),
    )

    assert first is not None
    assert first.token == "owner-one"
    assert second is None
    assert fake.values["lock:ticker:refresh"] == "owner-one"
    assert fake.commands == [
        ("set", "lock:ticker:refresh", "owner-one", 5, True),
        ("set", "lock:ticker:refresh", "owner-two", 5, True),
    ]


@pytest.mark.asyncio
async def test_default_lock_tokens_are_unique_uuid_hex_values() -> None:
    fake = FakeRedis()
    manager = RedisLockManager(RedisCache(fake))

    first = await manager.acquire(key="lock:key", ttl_seconds=5)
    assert first is not None
    assert await manager.release(first) is True
    second = await manager.acquire(key="lock:key", ttl_seconds=5)
    assert second is not None

    assert first.token != second.token
    assert len(first.token) == len(second.token) == 32
    assert int(first.token, 16) >= 0
    assert int(second.token, 16) >= 0


@pytest.mark.asyncio
async def test_only_lock_owner_can_release() -> None:
    fake = FakeRedis()
    cache = RedisCache(fake)
    first = RedisLockManager(cache, token_factory=lambda: "owner-one")
    other = RedisLockManager(cache, token_factory=lambda: "owner-two")
    first_lease = await first.acquire(key="lock:market:list", ttl_seconds=10)
    other_lease = await other.acquire(key="lock:market:list", ttl_seconds=10)

    assert first_lease is not None
    assert other_lease is None
    assert (
        await cache.compare_and_delete(
            key="lock:market:list", expected_value="owner-two"
        )
        is False
    )
    assert fake.values["lock:market:list"] == "owner-one"
    assert await first.release(first_lease) is True
    assert "lock:market:list" not in fake.values


@pytest.mark.asyncio
async def test_expired_owner_cannot_delete_new_owners_lock() -> None:
    fake = FakeRedis()
    manager = RedisLockManager(RedisCache(fake), token_factory=lambda: "old-owner")
    old_lease = await manager.acquire(key="lock:key", ttl_seconds=5)
    assert old_lease is not None

    fake.values["lock:key"] = "new-owner"
    fake.ttls["lock:key"] = 5

    assert await manager.release(old_lease) is False
    assert fake.values["lock:key"] == "new-owner"


@pytest.mark.asyncio
async def test_lock_context_releases_after_success_and_exception() -> None:
    fake = FakeRedis()
    manager = RedisLockManager(RedisCache(fake), token_factory=lambda: "token")

    async with manager.lock(key="lock:one", ttl_seconds=5) as lease:
        assert lease is not None
        assert fake.values["lock:one"] == "token"
    assert "lock:one" not in fake.values

    with pytest.raises(RuntimeError, match="original failure"):
        async with manager.lock(key="lock:two", ttl_seconds=5):
            raise RuntimeError("original failure")
    assert "lock:two" not in fake.values


@pytest.mark.asyncio
async def test_lock_context_releases_and_propagates_cancellation() -> None:
    fake = FakeRedis()
    manager = RedisLockManager(RedisCache(fake), token_factory=lambda: "token")

    with pytest.raises(asyncio.CancelledError):
        async with manager.lock(key="lock:cancel", ttl_seconds=5):
            raise asyncio.CancelledError

    assert "lock:cancel" not in fake.values


@pytest.mark.asyncio
async def test_release_failure_does_not_hide_original_error_or_log_private_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRedis()
    manager = RedisLockManager(RedisCache(fake), token_factory=lambda: "private-token")

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        with pytest.raises(RuntimeError, match="original failure"):
            async with manager.lock(key="lock:safe", ttl_seconds=5):
                fake.failures["eval"] = ConnectionError("private redis url")
                raise RuntimeError("original failure")

    assert "private-token" not in caplog.text
    assert "private redis url" not in caplog.text
    assert "lock:safe" in caplog.text


@pytest.mark.asyncio
async def test_wait_for_cache_rechecks_every_100ms_and_returns_first_hit() -> None:
    sleeps: list[float] = []
    reads = iter([None, None, {"ready": True}])

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    async def read_cache() -> dict[str, bool] | None:
        return next(reads)

    result = await wait_for_cache_refresh(read_cache, sleeper=sleeper)

    assert result == {"ready": True}
    assert sleeps == [0.1, 0.1, 0.1]


@pytest.mark.asyncio
async def test_wait_for_cache_stops_after_500ms_with_expected_app_error() -> None:
    sleeps: list[float] = []
    read_count = 0

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    async def read_cache() -> None:
        nonlocal read_count
        read_count += 1
        return None

    with pytest.raises(AppError) as exc_info:
        await wait_for_cache_refresh(read_cache, sleeper=sleeper)

    assert sleeps == [0.1] * 5
    assert read_count == 5
    assert exc_info.value.code is ErrorCode.CACHE_REFRESH_IN_PROGRESS
    assert exc_info.value.status_code == 503
    assert exc_info.value.details is None


@pytest.mark.asyncio
async def test_wait_for_cache_propagates_cancellation_without_extra_reads() -> None:
    reads = 0

    async def sleeper(seconds: float) -> None:
        raise asyncio.CancelledError

    async def read_cache() -> None:
        nonlocal reads
        reads += 1
        return None

    with pytest.raises(asyncio.CancelledError):
        await wait_for_cache_refresh(read_cache, sleeper=sleeper)

    assert reads == 0
