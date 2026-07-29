import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.cache.redis import (
    RedisCacheFactory,
    RedisUnavailableError,
    create_redis_cache,
)
from app.clients.upbit import UpbitClientFactory, create_upbit_client
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.exceptions import register_exception_handlers
from app.services.chart import ChartServiceFactory, create_chart_service
from app.services.market_list import (
    MarketListServiceFactory,
    create_market_list_service,
)
from app.services.price import PriceServiceFactory, create_price_service


logger = logging.getLogger("uvicorn.error")

ALLOWED_CORS_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
ALLOWED_CORS_HEADERS = ["Authorization", "Content-Type"]


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings: Settings = application.state.settings
    upbit_client = None
    redis_cache = None
    market_list_service = None
    price_service = None
    chart_service = None
    try:
        upbit_client = application.state.upbit_client_factory(settings)
        application.state.upbit_client = upbit_client
        redis_cache = application.state.redis_cache_factory(settings)
        application.state.redis_cache = redis_cache
        market_list_service = application.state.market_list_service_factory(
            upbit_client,
            redis_cache,
        )
        application.state.market_list_service = market_list_service
        price_service = application.state.price_service_factory(
            upbit_client,
            redis_cache,
        )
        application.state.price_service = price_service
        chart_service = application.state.chart_service_factory(
            upbit_client,
            redis_cache,
            market_list_service,
        )
        application.state.chart_service = chart_service
        if application.state.load_markets_on_startup:
            try:
                await market_list_service.get_markets()
            except (AppError, RedisUnavailableError):
                logger.warning("Initial market list load failed")
        logger.info("WatchUp application started")
        yield
    finally:
        if redis_cache is not None:
            try:
                await redis_cache.aclose()
            except Exception:
                logger.warning("Failed to close Redis client")
        if upbit_client is not None:
            try:
                await upbit_client.aclose()
            except Exception:
                logger.exception("Failed to close Upbit HTTP client")
        supabase_http_client = application.state.supabase_http_client
        if supabase_http_client is not None:
            try:
                supabase_http_client.close()
            except Exception:
                logger.exception("Failed to close Supabase HTTP client")
        logger.info("WatchUp application stopped")


def create_app(
    settings: Settings | None = None,
    *,
    upbit_client_factory: UpbitClientFactory = create_upbit_client,
    redis_cache_factory: RedisCacheFactory = create_redis_cache,
    market_list_service_factory: MarketListServiceFactory = create_market_list_service,
    price_service_factory: PriceServiceFactory = create_price_service,
    chart_service_factory: ChartServiceFactory = create_chart_service,
    load_markets_on_startup: bool = True,
) -> FastAPI:
    active_settings = settings or get_settings()
    application = FastAPI(title="WatchUp API", lifespan=lifespan)
    application.state.settings = active_settings
    application.state.upbit_client = None
    application.state.upbit_client_factory = upbit_client_factory
    application.state.redis_cache = None
    application.state.redis_cache_factory = redis_cache_factory
    application.state.jwt_verifier = None
    application.state.jwt_verifier_lock = Lock()
    application.state.supabase_http_client = None
    application.state.market_list_service = None
    application.state.market_list_service_factory = market_list_service_factory
    application.state.price_service = None
    application.state.price_service_factory = price_service_factory
    application.state.chart_service = None
    application.state.chart_service_factory = chart_service_factory
    application.state.load_markets_on_startup = load_markets_on_startup
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
