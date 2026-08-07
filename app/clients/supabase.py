"""검증된 사용자 token으로 RLS가 적용되는 Supabase Data API Client를 만든다."""

from typing import Final

import httpx
from supabase import Client, create_client
from supabase.client import ClientOptions

from app.core.config import Settings
from app.models.auth import AuthContext


SUPABASE_DATA_API_TIMEOUT_SECONDS: Final[float] = 10


class SupabaseConfigurationError(RuntimeError):
    """사용자 범위 Supabase Client를 구성할 서버 설정이 없을 때 발생한다."""


def create_user_supabase_client(
    *,
    settings: Settings,
    auth_context: AuthContext,
    http_client: httpx.Client,
) -> Client:
    """검증된 한 사용자의 token만 전달하는 격리된 Data API Client를 만든다."""
    supabase_url = settings.supabase_url.strip()
    anon_key = settings.supabase_anon_key.strip()
    if not supabase_url or not anon_key:
        raise SupabaseConfigurationError("Supabase Data API settings are incomplete")

    # anon key는 apikey header 역할만 유지하고 Authorization에는 사용자 JWT를 둔다.
    # 요청마다 새 options와 Client를 만들므로, 공유 HTTP 연결 풀을 사용하더라도
    # PostgREST/RLS가 판단하는 사용자 인증 상태는 요청 사이에 섞이지 않는다.
    options = ClientOptions(
        headers={
            "Authorization": f"Bearer {auth_context.access_token}",
        },
        auto_refresh_token=False,
        persist_session=False,
        httpx_client=http_client,
    )
    return create_client(
        supabase_url,
        anon_key,
        options=options,
    )
