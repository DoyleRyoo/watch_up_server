"""Compatibility entry point; prefer ``uvicorn app.main:app``."""

from app.main import app


__all__ = ["app"]
