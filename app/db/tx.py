from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from app.core.errors import AppError, ErrorCode
from app.db.pool import DatabasePool

ResultT = TypeVar("ResultT")
DATABASE_UNAVAILABLE_MESSAGE = (
    "일시적으로 요청을 처리할 수 없습니다. 잠시 후 다시 시도해주세요."
)


class DatabaseTransactionManager:
    def __init__(self, pool: DatabasePool, role: str) -> None:
        self._pool = pool
        self._role = role

    async def run(
        self,
        user_id: UUID,
        operation: Callable[[asyncpg.Connection], Awaitable[ResultT]],
    ) -> ResultT:
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction(isolation="read_committed"):
                    await connection.execute(f"set local role {self._role}")
                    await connection.execute(
                        "select set_config('request.jwt.claim.sub', $1, true)",
                        str(user_id),
                    )
                    await connection.execute(
                        "select set_config('request.jwt.claim.role', 'authenticated', true)"
                    )
                    return await operation(connection)
        except asyncpg.PostgresError as exc:
            if exc.sqlstate in {"40001", "40P01"}:
                raise AppError(
                    code=ErrorCode.DATABASE_UNAVAILABLE,
                    message=DATABASE_UNAVAILABLE_MESSAGE,
                ) from exc
            raise
