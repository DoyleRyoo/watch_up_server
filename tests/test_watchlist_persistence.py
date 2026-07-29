from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from postgrest import APIError, CountMethod
from pydantic import ValidationError

from app.core.errors import AppError, ErrorCode
from app.models.watchlist import WatchlistInsert, WatchlistRow
from app.repositories.watchlist import (
    WATCHLIST_COLUMNS,
    WatchlistDuplicateError,
    WatchlistNotFoundError,
    WatchlistRepository,
    WatchlistRepositoryError,
)
from app.services.watchlist import WatchlistService


USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CREATED_AT = "2026-07-27T12:34:56.123456+09:00"


def row_payload(*, row_id: object = 1, user_id: UUID = USER_ID) -> dict[str, object]:
    return {
        "id": row_id,
        "user_id": str(user_id),
        "market_code": "KRW-BTC",
        "korean_name": "비트코인",
        "english_name": "Bitcoin",
        "created_at": CREATED_AT,
    }


def insert_values() -> WatchlistInsert:
    return WatchlistInsert(
        user_id=USER_ID,
        market_code="KRW-BTC",
        korean_name="비트코인",
        english_name="Bitcoin",
    )


def test_models_parse_uuid_preserve_timezone_and_use_snake_case() -> None:
    row = WatchlistRow.model_validate(row_payload())

    assert row.user_id == USER_ID
    assert isinstance(row.user_id, UUID)
    assert row.created_at.utcoffset() == datetime.fromisoformat(CREATED_AT).utcoffset()
    assert row.created_at.isoformat() == CREATED_AT
    assert set(type(row).model_fields) == {
        "id",
        "user_id",
        "market_code",
        "korean_name",
        "english_name",
        "created_at",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "1"),
        ("id", 0),
        ("id", 9_223_372_036_854_775_808),
        ("created_at", "invalid"),
        ("created_at", "2026-07-27T12:34:56"),
    ],
)
def test_row_rejects_invalid_id_and_timestamp(field: str, value: object) -> None:
    payload = row_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        WatchlistRow.model_validate(payload)


@pytest.mark.parametrize(
    "market_code",
    ["BTC", "USD-BTC", "krw-btc", "KRW-", "KRW-B_TC", "KRW-BTC "],
)
def test_row_rejects_noncanonical_database_market_code(market_code: str) -> None:
    payload = row_payload()
    payload["market_code"] = market_code

    with pytest.raises(ValidationError):
        WatchlistRow.model_validate(payload)


def test_row_rejects_missing_and_extra_fields() -> None:
    missing = row_payload()
    del missing["created_at"]

    with pytest.raises(ValidationError):
        WatchlistRow.model_validate(missing)
    with pytest.raises(ValidationError):
        WatchlistRow.model_validate(row_payload() | {"current_price": 1})


def test_insert_model_allows_only_four_database_input_fields() -> None:
    values = insert_values()

    assert values.to_db_payload() == {
        "user_id": str(USER_ID),
        "market_code": "KRW-BTC",
        "korean_name": "비트코인",
        "english_name": "Bitcoin",
    }
    with pytest.raises(ValidationError):
        WatchlistInsert.model_validate(values.model_dump() | {"id": 1})
    with pytest.raises(ValidationError):
        WatchlistInsert.model_validate(values.model_dump() | {"created_at": CREATED_AT})
    with pytest.raises(ValidationError):
        WatchlistInsert.model_validate(values.model_dump() | {"current_price": 1})


@dataclass
class FakeResponse:
    data: object
    count: object = None


class FakeQuery:
    def __init__(
        self,
        response: FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or FakeResponse([])
        self.error = error
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def select(self, *columns: str, **options: object) -> "FakeQuery":
        self.calls.append(("select", columns, options))
        return self

    def insert(self, payload: dict[str, str]) -> "FakeQuery":
        self.calls.append(("insert", (payload,), {}))
        return self

    def delete(self) -> "FakeQuery":
        self.calls.append(("delete", (), {}))
        return self

    def eq(self, column: str, value: object) -> "FakeQuery":
        self.calls.append(("eq", (column, value), {}))
        return self

    def order(self, column: str, *, desc: bool = False) -> "FakeQuery":
        self.calls.append(("order", (column,), {"desc": desc}))
        return self

    def limit(self, size: int) -> "FakeQuery":
        self.calls.append(("limit", (size,), {}))
        return self

    def execute(self) -> FakeResponse:
        self.calls.append(("execute", (), {}))
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, query: FakeQuery) -> None:
        self.query = query
        self.tables: list[str] = []

    def table(self, name: str) -> FakeQuery:
        self.tables.append(name)
        return self.query


def test_list_filters_user_selects_columns_and_orders_stably() -> None:
    query = FakeQuery(FakeResponse([row_payload(), row_payload(row_id=2)]))
    client = FakeClient(query)

    rows = WatchlistRepository().list_by_user(
        client=client,  # type: ignore[arg-type]
        user_id=USER_ID,
    )

    assert client.tables == ["watchlist"]
    assert query.calls == [
        ("select", (WATCHLIST_COLUMNS,), {}),
        ("eq", ("user_id", str(USER_ID)), {}),
        ("order", ("created_at",), {"desc": False}),
        ("order", ("id",), {"desc": False}),
        ("execute", (), {}),
    ]
    assert [row.id for row in rows] == [1, 2]


def test_list_empty_result_is_empty_list() -> None:
    result = WatchlistRepository().list_by_user(
        client=FakeClient(FakeQuery(FakeResponse([]))),  # type: ignore[arg-type]
        user_id=USER_ID,
    )
    assert result == []


@pytest.mark.parametrize("data", [[{"id": 1}], None, {"id": 1}])
def test_list_rejects_invalid_response(data: object) -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().list_by_user(
            client=FakeClient(FakeQuery(FakeResponse(data))),  # type: ignore[arg-type]
            user_id=USER_ID,
        )


def test_list_does_not_hide_database_error_as_empty_list() -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().list_by_user(
            client=FakeClient(FakeQuery(error=RuntimeError("down"))),  # type: ignore[arg-type]
            user_id=USER_ID,
        )


@pytest.mark.parametrize("count", [0, 1, 50])
def test_count_uses_exact_head_query_without_downloading_rows(count: int) -> None:
    query = FakeQuery(FakeResponse([], count=count))

    result = WatchlistRepository().count_by_user(
        client=FakeClient(query),  # type: ignore[arg-type]
        user_id=USER_ID,
    )

    assert result == count
    assert query.calls == [
        ("select", ("id",), {"count": CountMethod.exact, "head": True}),
        ("eq", ("user_id", str(USER_ID)), {}),
        ("execute", (), {}),
    ]


@pytest.mark.parametrize("count", [None, "0", True, -1])
def test_count_rejects_missing_or_invalid_count(count: object) -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().count_by_user(
            client=FakeClient(FakeQuery(FakeResponse([], count=count))),  # type: ignore[arg-type]
            user_id=USER_ID,
        )


def test_count_does_not_hide_database_error_as_zero() -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().count_by_user(
            client=FakeClient(FakeQuery(error=RuntimeError("down"))),  # type: ignore[arg-type]
            user_id=USER_ID,
        )


@pytest.mark.parametrize(("data", "expected"), [([], False), ([{"id": 1}], True)])
def test_exists_applies_both_filters_and_limit(
    data: list[dict[str, object]], expected: bool
) -> None:
    query = FakeQuery(FakeResponse(data))

    result = WatchlistRepository().exists_by_user_and_market(
        client=FakeClient(query),  # type: ignore[arg-type]
        user_id=USER_ID,
        market_code="KRW-ETH",
    )

    assert result is expected
    assert query.calls == [
        ("select", ("id",), {}),
        ("eq", ("user_id", str(USER_ID)), {}),
        ("eq", ("market_code", "KRW-ETH"), {}),
        ("limit", (1,), {}),
        ("execute", (), {}),
    ]
    assert str(OTHER_USER_ID) not in repr(query.calls)


@pytest.mark.parametrize("data", [[{"bad": 1}], [{"id": 1}, {"id": 2}]])
def test_exists_rejects_invalid_response(data: list[dict[str, object]]) -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().exists_by_user_and_market(
            client=FakeClient(FakeQuery(FakeResponse(data))),  # type: ignore[arg-type]
            user_id=USER_ID,
            market_code="KRW-BTC",
        )


def test_exists_does_not_hide_database_error_as_false() -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().exists_by_user_and_market(
            client=FakeClient(FakeQuery(error=RuntimeError("down"))),  # type: ignore[arg-type]
            user_id=USER_ID,
            market_code="KRW-BTC",
        )


def test_insert_sends_only_allowed_payload_and_returns_database_values() -> None:
    query = FakeQuery(FakeResponse([row_payload(row_id=42)]))

    result = WatchlistRepository().insert(
        client=FakeClient(query),  # type: ignore[arg-type]
        values=insert_values(),
    )

    assert result.id == 42
    assert result.created_at.isoformat() == CREATED_AT
    assert query.calls == [
        ("insert", (insert_values().to_db_payload(),), {}),
        ("select", (WATCHLIST_COLUMNS,), {}),
        ("execute", (), {}),
    ]
    payload = query.calls[0][1][0]
    assert isinstance(payload, dict)
    assert set(payload) == {
        "user_id",
        "market_code",
        "korean_name",
        "english_name",
    }


@pytest.mark.parametrize(
    "data", [[], [row_payload(), row_payload(row_id=2)], [{"id": 1}]]
)
def test_insert_rejects_missing_multiple_or_invalid_rows(
    data: list[dict[str, object]],
) -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().insert(
            client=FakeClient(FakeQuery(FakeResponse(data))),  # type: ignore[arg-type]
            values=insert_values(),
        )


def api_error(code: str) -> APIError:
    return APIError({"code": code, "message": "private", "details": None, "hint": None})


def test_insert_maps_structured_23505_to_duplicate() -> None:
    with pytest.raises(WatchlistDuplicateError):
        WatchlistRepository().insert(
            client=FakeClient(FakeQuery(error=api_error("23505"))),  # type: ignore[arg-type]
            values=insert_values(),
        )


@pytest.mark.parametrize("error", [api_error("42501"), RuntimeError("down")])
def test_insert_keeps_other_database_failures_generic(error: Exception) -> None:
    with pytest.raises(WatchlistRepositoryError) as captured:
        WatchlistRepository().insert(
            client=FakeClient(FakeQuery(error=error)),  # type: ignore[arg-type]
            values=insert_values(),
        )
    assert type(captured.value) is WatchlistRepositoryError


def test_delete_filters_id_and_user_and_returns_matching_id() -> None:
    query = FakeQuery(FakeResponse([{"id": 31}]))

    result = WatchlistRepository().delete_by_user_and_id(
        client=FakeClient(query),  # type: ignore[arg-type]
        user_id=USER_ID,
        watchlist_id=31,
    )

    assert result == 31
    assert query.calls == [
        ("delete", (), {}),
        ("eq", ("id", 31), {}),
        ("eq", ("user_id", str(USER_ID)), {}),
        ("select", ("id",), {}),
        ("execute", (), {}),
    ]


def test_delete_treats_missing_or_rls_invisible_row_as_not_found() -> None:
    with pytest.raises(WatchlistNotFoundError):
        WatchlistRepository().delete_by_user_and_id(
            client=FakeClient(FakeQuery(FakeResponse([]))),  # type: ignore[arg-type]
            user_id=USER_ID,
            watchlist_id=31,
        )


@pytest.mark.parametrize(
    "data", [[{"id": 32}], [{"id": 31}, {"id": 31}], [{"id": "31"}]]
)
def test_delete_rejects_mismatched_multiple_or_invalid_ids(
    data: list[dict[str, object]],
) -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().delete_by_user_and_id(
            client=FakeClient(FakeQuery(FakeResponse(data))),  # type: ignore[arg-type]
            user_id=USER_ID,
            watchlist_id=31,
        )


def test_delete_does_not_hide_database_error_as_success() -> None:
    with pytest.raises(WatchlistRepositoryError):
        WatchlistRepository().delete_by_user_and_id(
            client=FakeClient(FakeQuery(error=RuntimeError("down"))),  # type: ignore[arg-type]
            user_id=USER_ID,
            watchlist_id=31,
        )


class FakeRepository:
    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def complete(self, name: str, arguments: dict[str, object]) -> Any:
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result

    def list_by_user(self, **arguments: object) -> Any:
        return self.complete("list", arguments)

    def count_by_user(self, **arguments: object) -> Any:
        return self.complete("count", arguments)

    def exists_by_user_and_market(self, **arguments: object) -> Any:
        return self.complete("exists", arguments)

    def insert(self, **arguments: object) -> Any:
        return self.complete("insert", arguments)

    def delete_by_user_and_id(self, **arguments: object) -> Any:
        return self.complete("delete", arguments)


def service(repository: FakeRepository) -> WatchlistService:
    return WatchlistService(repository=repository)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("method", "arguments", "result", "operation"),
    [
        ("list_for_user", {}, [], "list"),
        ("count_for_user", {}, 50, "count"),
        ("is_registered", {"market_code": "KRW-BTC"}, True, "exists"),
        ("delete_for_user", {"watchlist_id": 1}, 1, "delete"),
    ],
)
def test_service_reuses_scoped_client_and_verified_user(
    method: str,
    arguments: dict[str, object],
    result: object,
    operation: str,
) -> None:
    repository = FakeRepository(result=result)
    client = object()

    actual = getattr(service(repository), method)(
        client=client, user_id=USER_ID, **arguments
    )

    assert actual == result
    assert repository.calls == [
        (operation, {"client": client, "user_id": USER_ID, **arguments})
    ]
    assert OTHER_USER_ID not in repository.calls[0][1].values()


def test_service_builds_insert_from_verified_user() -> None:
    expected = WatchlistRow.model_validate(row_payload())
    repository = FakeRepository(result=expected)
    client = object()

    result = service(repository).add_for_user(
        client=client,  # type: ignore[arg-type]
        user_id=USER_ID,
        market_code="KRW-BTC",
        korean_name="비트코인",
        english_name="Bitcoin",
    )

    assert result == expected
    _, arguments = repository.calls[0]
    assert arguments["client"] is client
    values = arguments["values"]
    assert isinstance(values, WatchlistInsert)
    assert values.user_id == USER_ID


def test_service_maps_duplicate_and_not_found_without_private_details() -> None:
    duplicate = FakeRepository(error=WatchlistDuplicateError("private"))
    with pytest.raises(AppError) as captured_duplicate:
        service(duplicate).add_for_user(
            client=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            market_code="KRW-BTC",
            korean_name="비트코인",
            english_name="Bitcoin",
        )
    assert captured_duplicate.value.code is ErrorCode.WATCHLIST_DUPLICATED
    assert captured_duplicate.value.status_code == 409
    assert captured_duplicate.value.message == "이미 등록된 코인입니다."

    not_found = FakeRepository(error=WatchlistNotFoundError("private"))
    with pytest.raises(AppError) as captured_not_found:
        service(not_found).delete_for_user(
            client=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            watchlist_id=1,
        )
    assert captured_not_found.value.code is ErrorCode.WATCHLIST_NOT_FOUND
    assert captured_not_found.value.status_code == 404
    assert "private" not in captured_not_found.value.message


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("list_for_user", {}),
        ("count_for_user", {}),
        ("is_registered", {"market_code": "KRW-BTC"}),
        (
            "add_for_user",
            {
                "market_code": "KRW-BTC",
                "korean_name": "비트코인",
                "english_name": "Bitcoin",
            },
        ),
        ("delete_for_user", {"watchlist_id": 1}),
    ],
)
def test_service_maps_repository_errors_to_safe_500(
    method: str, arguments: dict[str, object]
) -> None:
    repository = FakeRepository(error=WatchlistRepositoryError("private-token"))

    with pytest.raises(AppError) as captured:
        getattr(service(repository), method)(
            client=object(), user_id=USER_ID, **arguments
        )

    assert captured.value.code is ErrorCode.INTERNAL_SERVER_ERROR
    assert captured.value.status_code == 500
    assert captured.value.message == "서버 내부 오류가 발생했습니다."
    assert "private-token" not in captured.value.message
