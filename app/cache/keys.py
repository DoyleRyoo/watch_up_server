"""Redis keys and TTLs shared by WatchUp services."""

from typing import Final


MARKET_LIST_KEY: Final = "market:list"
MARKET_LIST_TTL_SECONDS: Final = 24 * 60 * 60

PRICE_TTL_SECONDS: Final = 5
STALE_PRICE_TTL_SECONDS: Final = 60 * 60

CHART_PERIOD: Final = "1d"
CHART_TTL_SECONDS: Final = 5 * 60
STALE_CHART_TTL_SECONDS: Final = 24 * 60 * 60

MARKET_LIST_LOCK_KEY: Final = "lock:market:list"
MARKET_LIST_LOCK_TTL_SECONDS: Final = 10
TICKER_REFRESH_LOCK_KEY: Final = "lock:ticker:refresh"
TICKER_REFRESH_LOCK_TTL_SECONDS: Final = 5

LOCK_CACHE_RECHECK_INTERVAL_SECONDS: Final = 0.1
LOCK_CACHE_RECHECK_ATTEMPTS: Final = 5


def price_key(market_code: str) -> str:
    return f"price:{_validated_market_code(market_code)}"


def stale_price_key(market_code: str) -> str:
    return f"stale:price:{_validated_market_code(market_code)}"


def chart_key(market_code: str) -> str:
    return f"chart:{_validated_market_code(market_code)}:{CHART_PERIOD}"


def stale_chart_key(market_code: str) -> str:
    return f"stale:chart:{_validated_market_code(market_code)}:{CHART_PERIOD}"


def _validated_market_code(market_code: str) -> str:
    normalized = market_code.strip()
    if not normalized:
        raise ValueError("market_code must not be empty")
    return normalized


__all__ = [
    "CHART_PERIOD",
    "CHART_TTL_SECONDS",
    "LOCK_CACHE_RECHECK_ATTEMPTS",
    "LOCK_CACHE_RECHECK_INTERVAL_SECONDS",
    "MARKET_LIST_KEY",
    "MARKET_LIST_LOCK_KEY",
    "MARKET_LIST_LOCK_TTL_SECONDS",
    "MARKET_LIST_TTL_SECONDS",
    "PRICE_TTL_SECONDS",
    "STALE_CHART_TTL_SECONDS",
    "STALE_PRICE_TTL_SECONDS",
    "TICKER_REFRESH_LOCK_KEY",
    "TICKER_REFRESH_LOCK_TTL_SECONDS",
    "chart_key",
    "price_key",
    "stale_chart_key",
    "stale_price_key",
]
