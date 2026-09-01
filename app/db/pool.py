from collections.abc import Awaitable, Callable
from typing import TypeAlias

import asyncpg  # type: ignore[import-untyped]

from app.core.config import Settings

DatabasePool: TypeAlias = asyncpg.Pool
DatabasePoolFactory: TypeAlias = Callable[[Settings], Awaitable[DatabasePool | None]]


async def create_database_pool(settings: Settings) -> DatabasePool | None:
    if not settings.database_session_url:
        return None
    return await asyncpg.create_pool(
        dsn=settings.database_session_url,
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        command_timeout=10,
    )
