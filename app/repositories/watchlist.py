"""Supabase repository for user-owned watchlist rows."""

from typing import Final
from uuid import UUID

from postgrest import APIError, CountMethod
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from supabase import Client

from app.models.watchlist import (
    POSTGRES_BIGINT_MAX,
    WatchlistInsert,
    WatchlistRow,
)


WATCHLIST_TABLE: Final = "watchlist"
WATCHLIST_COLUMNS: Final = "id,user_id,market_code,korean_name,english_name,created_at"
POSTGRES_UNIQUE_VIOLATION: Final = "23505"


class WatchlistRepositoryError(RuntimeError):
    """Raised when a watchlist database operation cannot be trusted."""


class WatchlistDuplicateError(WatchlistRepositoryError):
    """Raised when the database UNIQUE constraint rejects an insert."""


class WatchlistNotFoundError(WatchlistRepositoryError):
    """Raised when RLS exposes no row matching a requested deletion."""


class _DeletedWatchlistRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: StrictInt = Field(gt=0, le=POSTGRES_BIGINT_MAX)


class WatchlistRepository:
    """Runs user-filtered queries with an already user-scoped client."""

    def list_by_user(
        self,
        *,
        client: Client,
        user_id: UUID,
    ) -> list[WatchlistRow]:
        try:
            response = (
                client.table(WATCHLIST_TABLE)
                .select(WATCHLIST_COLUMNS)
                .eq("user_id", str(user_id))
                .order("created_at", desc=False)
                .order("id", desc=False)
                .execute()
            )
        except Exception as exc:
            raise WatchlistRepositoryError("Failed to list watchlist rows") from exc

        data = self._response_data(response)
        try:
            return [WatchlistRow.model_validate(row) for row in data]
        except ValidationError as exc:
            raise WatchlistRepositoryError(
                "Supabase returned an invalid watchlist row"
            ) from exc

    def count_by_user(
        self,
        *,
        client: Client,
        user_id: UUID,
    ) -> int:
        try:
            response = (
                client.table(WATCHLIST_TABLE)
                .select("id", count=CountMethod.exact, head=True)
                .eq("user_id", str(user_id))
                .execute()
            )
        except Exception as exc:
            raise WatchlistRepositoryError("Failed to count watchlist rows") from exc

        count = getattr(response, "count", None)
        if type(count) is not int or count < 0:
            raise WatchlistRepositoryError("Supabase returned an invalid exact count")
        return count

    def exists_by_user_and_market(
        self,
        *,
        client: Client,
        user_id: UUID,
        market_code: str,
    ) -> bool:
        try:
            response = (
                client.table(WATCHLIST_TABLE)
                .select("id")
                .eq("user_id", str(user_id))
                .eq("market_code", market_code)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise WatchlistRepositoryError(
                "Failed to check for a watchlist row"
            ) from exc

        data = self._response_data(response)
        if len(data) > 1:
            raise WatchlistRepositoryError(
                "Supabase returned too many duplicate-check rows"
            )
        if not data:
            return False
        try:
            _DeletedWatchlistRow.model_validate(data[0])
        except ValidationError as exc:
            raise WatchlistRepositoryError(
                "Supabase returned an invalid duplicate-check row"
            ) from exc
        return True

    def insert(
        self,
        *,
        client: Client,
        values: WatchlistInsert,
    ) -> WatchlistRow:
        try:
            response = (
                client.table(WATCHLIST_TABLE)
                .insert(values.to_db_payload())
                .select(WATCHLIST_COLUMNS)
                .execute()
            )
        except APIError as exc:
            if exc.code == POSTGRES_UNIQUE_VIOLATION:
                raise WatchlistDuplicateError(
                    "Watchlist UNIQUE constraint rejected the insert"
                ) from exc
            raise WatchlistRepositoryError(
                "Supabase rejected the watchlist insert"
            ) from exc
        except Exception as exc:
            raise WatchlistRepositoryError("Failed to insert a watchlist row") from exc

        data = self._response_data(response)
        if len(data) != 1:
            raise WatchlistRepositoryError(
                "Supabase did not return exactly one inserted row"
            )
        try:
            return WatchlistRow.model_validate(data[0])
        except ValidationError as exc:
            raise WatchlistRepositoryError(
                "Supabase returned an invalid inserted row"
            ) from exc

    def delete_by_user_and_id(
        self,
        *,
        client: Client,
        user_id: UUID,
        watchlist_id: int,
    ) -> int:
        try:
            response = (
                client.table(WATCHLIST_TABLE)
                .delete()
                .eq("id", watchlist_id)
                .eq("user_id", str(user_id))
                .select("id")
                .execute()
            )
        except Exception as exc:
            raise WatchlistRepositoryError("Failed to delete a watchlist row") from exc

        data = self._response_data(response)
        if not data:
            raise WatchlistNotFoundError(
                "No visible watchlist row matched the deletion"
            )
        if len(data) != 1:
            raise WatchlistRepositoryError("Supabase returned too many deleted rows")
        try:
            deleted = _DeletedWatchlistRow.model_validate(data[0])
        except ValidationError as exc:
            raise WatchlistRepositoryError(
                "Supabase returned an invalid deleted row"
            ) from exc
        if deleted.id != watchlist_id:
            raise WatchlistRepositoryError("Supabase returned a different deleted row")
        return deleted.id

    @staticmethod
    def _response_data(response: object) -> list[object]:
        data = getattr(response, "data", None)
        if not isinstance(data, list):
            raise WatchlistRepositoryError(
                "Supabase returned an invalid response payload"
            )
        return data


__all__ = [
    "POSTGRES_UNIQUE_VIOLATION",
    "WATCHLIST_COLUMNS",
    "WATCHLIST_TABLE",
    "WatchlistDuplicateError",
    "WatchlistNotFoundError",
    "WatchlistRepository",
    "WatchlistRepositoryError",
]
