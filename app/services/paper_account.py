from dataclasses import dataclass
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from app.core.errors import AppError, ErrorCode
from app.db.tx import DatabaseTransactionManager
from app.repositories.paper_account import PaperAccountRepository, transaction_from_row
from app.schemas.paper import (
    TOP_UP_LIFETIME_CAP_KRW,
    TOP_UP_MAX_KRW,
    TOP_UP_MIN_KRW,
    PaperAccount,
    PaperTransaction,
)
from app.services.idempotency import ensure_matching_fingerprint, request_fingerprint


@dataclass(frozen=True, slots=True)
class TopUpResult:
    transaction: PaperTransaction
    replayed: bool


class PaperAccountService:
    def __init__(
        self,
        transactions: DatabaseTransactionManager,
        repository: PaperAccountRepository | None = None,
    ) -> None:
        self._transactions = transactions
        self._repository = repository or PaperAccountRepository()

    async def get_account(self, user_id: UUID) -> PaperAccount:
        async def operation(connection: asyncpg.Connection) -> PaperAccount:
            await self._repository.get_or_create(connection, user_id)
            return self._repository.serialize_account(
                await self._repository.read_account(connection, user_id)
            )

        return await self._transactions.run(user_id, operation)

    async def top_up(self, user_id: UUID, amount_krw: str, key: UUID) -> TopUpResult:
        amount = int(amount_krw)
        fingerprint = request_fingerprint(
            "/api/paper/top-ups", {"amountKrw": amount_krw}
        )

        async def operation(connection: asyncpg.Connection) -> TopUpResult:
            existing = await self._repository.find_transaction(connection, user_id, key)
            if existing is not None:
                ensure_matching_fingerprint(
                    existing["request_fingerprint"], fingerprint
                )
                return TopUpResult(transaction_from_row(existing), True)
            account = await self._repository.get_or_create(connection, user_id)
            existing = await self._repository.find_transaction(connection, user_id, key)
            if existing is not None:
                ensure_matching_fingerprint(
                    existing["request_fingerprint"], fingerprint
                )
                return TopUpResult(transaction_from_row(existing), True)
            if not TOP_UP_MIN_KRW <= amount <= TOP_UP_MAX_KRW:
                raise AppError(
                    code=ErrorCode.TOP_UP_AMOUNT_OUT_OF_RANGE,
                    message="1회 충전 가능 금액 범위를 벗어났습니다.",
                )
            if account.lifetime_top_up_krw + amount > TOP_UP_LIFETIME_CAP_KRW:
                raise AppError(
                    code=ErrorCode.TOP_UP_LIFETIME_LIMIT_EXCEEDED,
                    message="평생 누적 충전 한도를 초과했습니다.",
                )
            updated = await self._repository.update_for_top_up(
                connection, account, amount
            )
            return TopUpResult(
                await self._repository.insert_top_up(
                    connection, updated, amount, key, fingerprint
                ),
                False,
            )

        return await self._transactions.run(user_id, operation)
