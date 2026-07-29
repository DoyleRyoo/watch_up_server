from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Identity established from a verified Supabase access token."""

    user_id: UUID
    access_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.access_token:
            raise ValueError("access_token must not be empty")
