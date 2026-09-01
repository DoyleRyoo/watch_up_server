import hashlib
from uuid import UUID, uuid4

import pytest

from app.core.errors import AppError, ErrorCode
from app.services.idempotency import (
    ensure_matching_fingerprint,
    parse_idempotency_key,
    request_fingerprint,
)


def test_fingerprint_is_canonical_and_endpoint_scoped() -> None:
    left = request_fingerprint("/api/paper/top-ups", {"amountKrw": "1000"})
    right = request_fingerprint("/api/paper/top-ups", dict(amountKrw="1000"))
    assert left == right
    assert len(left) == 64
    assert (
        left
        == hashlib.sha256(
            b'{"body":{"amountKrw":"1000"},"endpoint":"/api/paper/top-ups"}'
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not-a-uuid",
        "00000000-0000-1000-8000-000000000000",
    ],
)
def test_idempotency_key_requires_canonical_uuid_v4(value: str | None) -> None:
    with pytest.raises(AppError) as raised:
        parse_idempotency_key(value)
    assert raised.value.code is ErrorCode.IDEMPOTENCY_KEY_REQUIRED


def test_uppercase_canonical_uuid_v4_is_accepted_case_insensitively() -> None:
    value = str(uuid4()).upper()
    assert parse_idempotency_key(value) == UUID(value)


def test_reused_key_requires_matching_fingerprint() -> None:
    ensure_matching_fingerprint("same", "same")
    with pytest.raises(AppError) as raised:
        ensure_matching_fingerprint("old", "new")
    assert raised.value.code is ErrorCode.IDEMPOTENCY_KEY_REUSED
