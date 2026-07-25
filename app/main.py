import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers


logger = logging.getLogger("uvicorn.error")

ALLOWED_CORS_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
ALLOWED_CORS_HEADERS = ["Authorization", "Content-Type"]


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    logger.info("WatchUp application started")
    # Shared network clients are created lazily by their dependencies.
    try:
        yield
    finally:
        supabase_http_client = application.state.supabase_http_client
        if supabase_http_client is not None:
            supabase_http_client.close()
        logger.info("WatchUp application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    application = FastAPI(title="WatchUp API", lifespan=lifespan)
    application.state.settings = active_settings
    application.state.jwt_verifier = None
    application.state.jwt_verifier_lock = Lock()
    application.state.supabase_http_client = None
    application.state.supabase_http_client_lock = Lock()

    application.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=ALLOWED_CORS_METHODS,
        allow_headers=ALLOWED_CORS_HEADERS,
    )
    register_exception_handlers(application)
    application.include_router(api_router, prefix="/api")

    return application


app = create_app()
