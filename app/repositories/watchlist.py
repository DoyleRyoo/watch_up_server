"""사용자 소유 관심 목록 행을 다루는 Supabase repository."""

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
    """관심 목록 DB 연산 결과를 신뢰할 수 없을 때 발생한다."""


class WatchlistDuplicateError(WatchlistRepositoryError):
    """DB UNIQUE 제약이 동시 중복 INSERT를 거부했음을 나타낸다."""


class WatchlistNotFoundError(WatchlistRepositoryError):
    """RLS를 통과해 삭제할 수 있는 대상 행이 보이지 않음을 나타낸다."""


class _DeletedWatchlistRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: StrictInt = Field(gt=0, le=POSTGRES_BIGINT_MAX)


class WatchlistRepository:
    """이미 사용자 token이 적용된 Client로 명시적 사용자 filter를 한 번 더 적용한다.

    애플리케이션 filter는 실수를 줄이고 DB RLS는 최종 권한 경계를 담당한다. 일반
    요청에서 service role로 RLS를 우회하지 않는다.
    """

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
            # RLS 때문에 존재하지 않는 id와 타 사용자 소유 id는 모두 보이지 않는다.
            # 권한 우회 조회를 추가하지 않고 둘 다 공개 404 계약으로 처리한다.
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
