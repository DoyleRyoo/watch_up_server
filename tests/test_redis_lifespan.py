import logging

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.cache.redis import RedisCache
from app.core.config import Settings
from app.main import create_app
from tests.test_redis_cache import FakeRedis


def test_lifespan_creates_one_lazy_pool_health_sends_no_redis_command() -> None:
    fake = FakeRedis()
    fake.failures.update(
        {
            "get": ConnectionError("Redis is down"),
            "mget": ConnectionError("Redis is down"),
            "set": ConnectionError("Redis is down"),
        }
    )
    cache = RedisCache(fake)
    created: list[RedisCache] = []

    def redis_factory(settings: Settings) -> RedisCache:
        created.append(cache)
        return cache

    application = create_app(
        Settings(_env_file=None),
        redis_cache_factory=redis_factory,
    )
    assert application.state.redis_cache is None

    with TestClient(application) as client:
        shared_cache = application.state.redis_cache
        first = client.get("/api/health")
        second = client.get("/api/health")
        assert application.state.redis_cache is shared_cache is cache
        assert fake.commands == []

    assert first.status_code == second.status_code == 200
    assert first.json() == {"data": {"status": "ok"}, "meta": None}
    assert created == [cache]
    assert fake.closed is True
    assert fake.commands == [("aclose",)]


def test_redis_close_failure_does_not_skip_other_lifespan_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UpbitClientStub:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class SupabaseClientStub:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake = FakeRedis()
    fake.failures["aclose"] = ConnectionError("private redis://user:pass@host")
    cache = RedisCache(fake)
    upbit = UpbitClientStub()
    supabase = SupabaseClientStub()

    application = create_app(
        Settings(_env_file=None),
        upbit_client_factory=lambda settings: upbit,
        redis_cache_factory=lambda settings: cache,
    )
    application.state.supabase_http_client = supabase

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        with TestClient(application) as client:
            assert client.get("/api/health").status_code == 200

    assert upbit.closed is True
    assert supabase.closed is True
    assert "private redis" not in caplog.text
