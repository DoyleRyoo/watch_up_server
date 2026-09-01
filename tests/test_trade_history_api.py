from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context
from app.api.dependencies.services import get_paper_history_service
from app.core.config import Settings
from app.main import create_app
from app.models.auth import AuthContext
from app.schemas.paper import PaperTransaction, PaperTransactionType
from app.services.paper_history import PaperHistoryPage, PaperHistoryService

USER_ID = uuid4()


def transaction(identifier: int, kind: PaperTransactionType) -> PaperTransaction:
    return PaperTransaction(
        id=str(identifier),
        type=kind,
        asset_class=None,
        market_code=None,
        execution_price=None,
        quantity=None,
        cash_delta_krw="1000",
        balance_after_krw="1001000",
        disposed_cost_basis_krw=None,
        realized_pnl_krw=None,
        quoted_at=None,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


class FakeHistoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int, int | None]] = []

    async def get_trades(
        self, user_id: UUID, *, limit: int, before_id: int | None
    ) -> PaperHistoryPage:
        self.calls.append((user_id, limit, before_id))
        return PaperHistoryPage(
            (
                transaction(4, PaperTransactionType.SELL),
                transaction(3, PaperTransactionType.BUY),
                transaction(2, PaperTransactionType.TOP_UP),
                transaction(1, PaperTransactionType.INITIAL_GRANT),
            )[:limit],
            has_more=limit < 4,
        )


def history_client() -> tuple[TestClient, FakeHistoryService]:
    app = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    service = FakeHistoryService()
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=USER_ID, access_token="token"
    )
    app.dependency_overrides[get_paper_history_service] = lambda: service
    return TestClient(app), service


def test_history_returns_all_types_with_cursor_metadata() -> None:
    client, service = history_client()
    with client:
        response = client.get("/api/paper/trades?limit=4&beforeId=10")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["data"]] == ["4", "3", "2", "1"]
    assert {item["type"] for item in payload["data"]} == {
        "INITIAL_GRANT",
        "TOP_UP",
        "BUY",
        "SELL",
    }
    assert payload["meta"] == {"count": 4, "hasMore": False}
    assert service.calls == [(USER_ID, 4, 10)]


def test_history_defaults_limit_and_rejects_invalid_query() -> None:
    client, service = history_client()
    with client:
        valid = client.get("/api/paper/trades")
        invalid = [
            client.get("/api/paper/trades?limit=0"),
            client.get("/api/paper/trades?limit=101"),
            client.get("/api/paper/trades?limit=abc"),
            client.get("/api/paper/trades?beforeId=x"),
            client.get("/api/paper/trades?beforeId=0"),
        ]

    assert valid.status_code == 200
    assert service.calls == [(USER_ID, 20, None)]
    for response in invalid:
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


class Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.query: tuple[str, tuple[object, ...]] | None = None

    async def fetch(self, sql: str, *args: object):
        self.query = (sql, args)
        return self.rows


class Transactions:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def run(self, user_id, operation):
        return await operation(self.connection)


class Accounts:
    def __init__(self) -> None:
        self.users: list[UUID] = []

    async def get_or_create(self, connection, user_id):
        self.users.append(user_id)


def row(identifier: int) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "BUY",
        "asset_class": "CRYPTO",
        "market_code": "KRW-BTC",
        "execution_price": None,
        "quantity": None,
        "cash_delta_krw": -1,
        "balance_after_krw": 999999,
        "disposed_cost_basis_krw": None,
        "realized_pnl_krw": None,
        "quoted_at": None,
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "request_fingerprint": None,
    }


@pytest.mark.asyncio
async def test_service_fetches_limit_plus_one_trims_and_uses_older_cursor() -> None:
    connection = Connection([row(9), row(8), row(7)])
    accounts = Accounts()
    service = PaperHistoryService(Transactions(connection), accounts)  # type: ignore[arg-type]

    page = await service.get_trades(USER_ID, limit=2, before_id=10)

    assert [item.id for item in page.transactions] == ["9", "8"]
    assert page.has_more is True
    assert accounts.users == [USER_ID]
    assert connection.query is not None
    sql, args = connection.query
    assert "account_id = $1 and id < $2" in sql
    assert "order by id desc limit $3" in sql
    assert args == (USER_ID, 10, 3)


class Ledger:
    """Fake `paper_transactions` honouring the account filter, cursor and limit."""

    def __init__(self, owners: dict[int, UUID]) -> None:
        self.owners = owners

    async def fetch(self, sql: str, *args: object):
        account_id = args[0]
        before_id = args[1] if "id < $2" in sql else None
        limit = args[-1]
        assert isinstance(limit, int)
        identifiers = sorted(
            (
                identifier
                for identifier, owner in self.owners.items()
                if owner == account_id
                and (before_id is None or identifier < int(before_id))  # type: ignore[arg-type]
            ),
            reverse=True,
        )
        return [row(identifier) for identifier in identifiers[:limit]]


def ledger_service(owners: dict[int, UUID]) -> PaperHistoryService:
    return PaperHistoryService(Transactions(Ledger(owners)), Accounts())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_twenty_five_transactions_paginate_over_two_default_pages() -> None:
    service = ledger_service({identifier: USER_ID for identifier in range(1, 26)})

    first = await service.get_trades(USER_ID, limit=20, before_id=None)
    assert len(first.transactions) == 20
    assert first.has_more is True
    assert [item.id for item in first.transactions] == [
        str(identifier) for identifier in range(25, 5, -1)
    ]

    cursor = int(first.transactions[-1].id)
    second = await service.get_trades(USER_ID, limit=20, before_id=cursor)
    assert [item.id for item in second.transactions] == ["5", "4", "3", "2", "1"]
    assert second.has_more is False


@pytest.mark.asyncio
async def test_history_never_returns_another_users_transactions() -> None:
    other_user = uuid4()
    service = ledger_service({1: USER_ID, 2: other_user, 3: USER_ID, 4: other_user})

    mine = await service.get_trades(USER_ID, limit=20, before_id=None)
    theirs = await service.get_trades(other_user, limit=20, before_id=None)

    assert [item.id for item in mine.transactions] == ["3", "1"]
    assert [item.id for item in theirs.transactions] == ["4", "2"]
