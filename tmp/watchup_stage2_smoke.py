from typing import Annotated

from fastapi import Depends
from supabase import Client

from app.api.dependencies.auth import (
    get_auth_context,
    get_supabase_client,
)
from app.main import create_app
from app.models.auth import AuthContext


app = create_app()


@app.get("/test/auth")
def auth_smoke(
    auth_context: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict[str, str]:
    return {"userId": str(auth_context.user_id)}


@app.get("/test/rls")
def rls_smoke(
    auth_context: Annotated[AuthContext, Depends(get_auth_context)],
    client: Annotated[Client, Depends(get_supabase_client)],
) -> dict[str, object]:
    response = client.table("watchlist").select("user_id").execute()
    rows = response.data

    return {
        "count": len(rows),
        "hasRows": bool(rows),
        "allOwnedByToken": all(
            row["user_id"] == str(auth_context.user_id)
            for row in rows
        ),
    }