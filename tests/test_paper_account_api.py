from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context
from app.api.dependencies.services import get_paper_account_service
from app.core.config import Settings
from app.main import create_app
from app.models.auth import AuthContext
from app.schemas.paper import PaperAccount, PaperTransaction, PaperTransactionType
from app.services.paper_account import TopUpResult

USER_ID = uuid4()


class FakePaperService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, UUID]] = []

    async def get_account(self, user_id: UUID) -> PaperAccount:
        assert user_id == USER_ID
        return PaperAccount(cash_balance_krw="1000000", lifetime_top_up_krw="0")

    async def top_up(self, user_id: UUID, amount_krw: str, key: UUID) -> TopUpResult:
        self.calls.append((user_id, amount_krw, key))
        transaction = PaperTransaction(
            id="2",
            type=PaperTransactionType.TOP_UP,
            asset_class=None,
            market_code=None,
            execution_price=None,
            quantity=None,
            cash_delta_krw=amount_krw,
            balance_after_krw="1001000",
            disposed_cost_basis_krw=None,
            realized_pnl_krw=None,
            quoted_at=None,
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        return TopUpResult(transaction, replayed=len(self.calls) > 1)


def _client() -> tuple[TestClient, FakePaperService]:
    app = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    service = FakePaperService()
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=USER_ID, access_token="token"
    )
    app.dependency_overrides[get_paper_account_service] = lambda: service
    return TestClient(app), service


def test_get_account_uses_verified_user_and_serializes_money_as_strings() -> None:
    client, _ = _client()
    with client:
        response = client.get("/api/paper/account")
    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "cashBalanceKrw": "1000000",
            "lifetimeTopUpKrw": "0",
            "topUpMinKrw": "1",
            "topUpMaxKrw": "2100000000",
            "topUpLifetimeCapKrw": "100000000000",
        },
        "meta": None,
    }


def test_top_up_returns_201_then_200_replay() -> None:
    client, service = _client()
    key = str(uuid4())
    with client:
        first = client.post(
            "/api/paper/top-ups",
            headers={"Idempotency-Key": key},
            json={"amountKrw": "1000"},
        )
        replay = client.post(
            "/api/paper/top-ups",
            headers={"Idempotency-Key": key},
            json={"amountKrw": "1000"},
        )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert service.calls == [(USER_ID, "1000", UUID(key)), (USER_ID, "1000", UUID(key))]


def test_top_up_rejects_number_unknown_fields_bad_amount_and_missing_key() -> None:
    client, service = _client()
    key = str(uuid4())
    bodies = [
        {"amountKrw": 1000},
        {"amountKrw": "01"},
        {"amountKrw": "1", "userId": str(USER_ID)},
        {"amountKrw": "1", "price": "1"},
    ]
    with client:
        for body in bodies:
            response = client.post(
                "/api/paper/top-ups", headers={"Idempotency-Key": key}, json=body
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
        missing = client.post("/api/paper/top-ups", json={"amountKrw": "1"})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert service.calls == []
