from typing import Final

import httpx
from supabase import Client, create_client
from supabase.client import ClientOptions

from app.core.config import Settings
from app.models.auth import AuthContext


SUPABASE_DATA_API_TIMEOUT_SECONDS: Final[float] = 10


class SupabaseConfigurationError(RuntimeError):
    """Raised when a user-scoped Supabase client cannot be configured."""


def create_user_supabase_client(
    *,
    settings: Settings,
    auth_context: AuthContext,
    http_client: httpx.Client,
) -> Client:
    """Create an isolated Data API client carrying one verified user token."""
    supabase_url = settings.supabase_url.strip()
    anon_key = settings.supabase_anon_key.strip()
    if not supabase_url or not anon_key:
        raise SupabaseConfigurationError("Supabase Data API settings are incomplete")

    # ClientOptions is the SDK's public construction API. Supplying the user JWT
    # here keeps the anon key in the apikey header while replacing only the
    # Authorization credential used by PostgREST/RLS. A fresh options object and
    # Client are created for every request, so authentication state is never shared.
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
