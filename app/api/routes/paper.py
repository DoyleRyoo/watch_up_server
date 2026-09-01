from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from app.api.dependencies.auth import get_auth_context
from app.api.dependencies.services import get_paper_account_service
from app.models.auth import AuthContext
from app.schemas.common import SuccessResponse
from app.schemas.paper import PaperAccount, PaperTransaction, TopUpRequest
from app.services.idempotency import parse_idempotency_key
from app.services.paper_account import PaperAccountService

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/account", response_model=SuccessResponse[PaperAccount])
async def get_account(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[PaperAccountService, Depends(get_paper_account_service)],
) -> SuccessResponse[PaperAccount]:
    return SuccessResponse(data=await service.get_account(auth.user_id))


@router.post(
    "/top-ups",
    response_model=SuccessResponse[PaperTransaction],
    status_code=status.HTTP_201_CREATED,
)
async def top_up(
    body: TopUpRequest,
    response: Response,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[PaperAccountService, Depends(get_paper_account_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SuccessResponse[PaperTransaction]:
    result = await service.top_up(
        auth.user_id, body.amount_krw, parse_idempotency_key(idempotency_key)
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return SuccessResponse(data=result.transaction)
