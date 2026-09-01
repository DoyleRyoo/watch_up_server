from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from app.core.errors import AppError, ErrorCode
from app.db.tx import DatabaseTransactionManager


class AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, error: Exception | None = None) -> None:
        self.commands: list[tuple[str, tuple[object, ...]]] = []
        self.error = error
        self.isolation: str | None = None

    def transaction(self, *, isolation: str) -> AsyncContext:
        self.isolation = isolation
        return AsyncContext(None)

    async def execute(self, sql: str, *args: object) -> str:
        self.commands.append((sql, args))
        if self.error is not None:
            raise self.error
        return "SELECT 1"


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> AsyncContext:
        return AsyncContext(self.connection)


@pytest.mark.asyncio
async def test_transaction_sets_authenticated_rls_context_from_verified_user() -> None:
    user_id = uuid4()
    connection = FakeConnection()
    manager = DatabaseTransactionManager(FakePool(connection), "authenticated")  # type: ignore[arg-type]

    result = await manager.run(user_id, lambda current: _result(current, connection))

    assert result == "ok"
    assert connection.isolation == "read_committed"
    assert connection.commands == [
        ("set local role authenticated", ()),
        ("select set_config('request.jwt.claim.sub', $1, true)", (str(user_id),)),
        ("select set_config('request.jwt.claim.role', 'authenticated', true)", ()),
    ]


async def _result(current: object, expected: object) -> str:
    assert current is expected
    return "ok"


@pytest.mark.asyncio
async def test_serialization_failure_maps_to_database_unavailable_without_retry() -> (
    None
):
    connection = FakeConnection(asyncpg.SerializationError("retry externally"))
    manager = DatabaseTransactionManager(FakePool(connection), "authenticated")  # type: ignore[arg-type]

    with pytest.raises(AppError) as raised:
        await manager.run(uuid4(), lambda current: _result(current, connection))

    assert raised.value.code is ErrorCode.DATABASE_UNAVAILABLE
    assert len(connection.commands) == 1
