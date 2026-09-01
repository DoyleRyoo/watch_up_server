import hashlib
import json
from uuid import UUID

from app.core.errors import AppError, ErrorCode

REQUIRED_MESSAGE = "Idempotency-Key 헤더가 필요합니다."
REUSED_MESSAGE = "이미 사용된 Idempotency-Key이며 이전 요청과 내용이 다릅니다."


def parse_idempotency_key(value: str | None) -> UUID:
    try:
        parsed = UUID(value) if value is not None else None
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.version != 4
        or value is None
        or value.casefold() != str(parsed)
    ):
        raise AppError(
            code=ErrorCode.IDEMPOTENCY_KEY_REQUIRED, message=REQUIRED_MESSAGE
        )
    return parsed


def request_fingerprint(endpoint: str, body: dict[str, str]) -> str:
    canonical = json.dumps(
        {"endpoint": endpoint, "body": body}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def ensure_matching_fingerprint(stored: str | None, expected: str) -> None:
    if stored != expected:
        raise AppError(code=ErrorCode.IDEMPOTENCY_KEY_REUSED, message=REUSED_MESSAGE)
