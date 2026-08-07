from typing import Any, Generic, TypeVar

from pydantic import Field, model_validator

from app.core.errors import ErrorCode
from app.schemas.base import APIModel


DataT = TypeVar("DataT")


class SuccessResponse(APIModel, Generic[DataT]):
    """Envelope for a single successful result."""

    data: DataT
    meta: None = None


class ListMeta(APIModel):
    count: int = Field(ge=0)


class ListResponse(APIModel, Generic[DataT]):
    """Envelope whose count is derived from and checked against its data."""

    data: list[DataT]
    meta: ListMeta

    @model_validator(mode="before")
    @classmethod
    def populate_count(cls, value: Any) -> Any:
        if isinstance(value, dict) and "meta" not in value:
            data = value.get("data")
            if isinstance(data, list):
                value = {**value, "meta": {"count": len(data)}}
        return value

    @model_validator(mode="after")
    def validate_count(self) -> "ListResponse[DataT]":
        if self.meta.count != len(self.data):
            raise ValueError("meta.count must match the number of data items")
        return self


class ErrorContent(APIModel):
    code: ErrorCode
    message: str
    details: Any | None = None


class ErrorResponse(APIModel):
    error: ErrorContent
