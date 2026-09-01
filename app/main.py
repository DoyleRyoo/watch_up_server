"""FastAPI 앱 구성과 프로세스 수명의 외부 연결을 조립한다."""

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
from app.clients.upbit_trade_price import TradePriceService
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.exceptions import register_exception_handlers
from app.db.pool import DatabasePoolFactory, create_database_pool
from app.db.tx import DatabaseTransactionManager
from app.services.chart import ChartServiceFactory, create_chart_service
from app.services.market_list import (
    MarketListServiceFactory,
    create_market_list_service,
)
from app.services.price import PriceServiceFactory, create_price_service
from app.services.paper_account import PaperAccountService
from app.services.paper_trade import PaperTradeService


logger = logging.getLogger("uvicorn.error")

ALLOWED_CORS_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
ALLOWED_CORS_HEADERS = ["Authorization", "Content-Type", "Idempotency-Key"]


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """프로세스별 공유 client와 service를 한 번 만들고 역순으로 정리한다.

    마켓 목록의 초기 적재는 검색 성능을 위한 준비 작업이므로 실패해도 서버 기동과
    `/api/health`를 막지 않는다. 실제 검색 시 동일 service가 다시 적재를 시도한다.
    """
    settings: Settings = application.state.settings
    upbit_client = None
    redis_cache = None
    market_list_service = None
    price_service = None
    chart_service = None
    database_pool = None
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
        database_pool = await application.state.database_pool_factory(settings)
        application.state.database_pool = database_pool
        if database_pool is not None:
            application.state.paper_account_service = PaperAccountService(
                DatabaseTransactionManager(database_pool, settings.database_role)
            )
            application.state.paper_trade_service = PaperTradeService(
                DatabaseTransactionManager(database_pool, settings.database_role),
                market_list_service,
                TradePriceService(upbit_client),
            )
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
        if database_pool is not None:
            await database_pool.close()
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
    database_pool_factory: DatabasePoolFactory = create_database_pool,
    load_markets_on_startup: bool = True,
) -> FastAPI:
    """설정과 교체 가능한 factory를 연결한 WatchUp 애플리케이션을 만든다.

    factory 인자는 테스트가 실제 Redis·Upbit에 연결하지 않고도 lifespan과 라우팅
    계약을 검증할 수 있게 하며, 운영에서는 모두 기본 구현을 사용한다.
    """
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
    application.state.database_pool = None
    application.state.database_pool_factory = database_pool_factory
    application.state.paper_account_service = None
    application.state.paper_trade_service = None
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
