"""Money request endpoints — the collect-money flow.

NOTE: no `from __future__ import annotations` here — see the comment in
`routers/auth.py`. It breaks slowapi's decorator and turns the request body
into a query parameter.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.orm import Session as DbSession

from app.core.deps import client_ip, get_current_user
from app.core.limiter import REQUEST_LIMIT, limiter
from app.database import get_db
from app.models import MoneyRequest, User
from app.schemas.money_request import (
    MoneyRequestCreate,
    MoneyRequestList,
    MoneyRequestOut,
    RespondToRequest,
)
from app.schemas.transfer import PartyOut
from app.services import request_service

router = APIRouter(prefix="/api", tags=["requests"])


def _to_out(req: MoneyRequest, viewer: User) -> MoneyRequestOut:
    incoming = req.payer_id == viewer.id
    other = req.requester if incoming else req.payer
    return MoneyRequestOut(
        id=req.id,
        direction="INCOMING" if incoming else "OUTGOING",
        counterparty=PartyOut(phone=other.phone, full_name=other.full_name),
        amount=req.amount,
        note=req.note,
        status=request_service.effective_status(req).value,
        transaction_reference=req.transaction.reference if req.transaction else None,
        expires_at=req.expires_at,
        created_at=req.created_at,
        responded_at=req.responded_at,
    )


@router.post(
    "/requests",
    response_model=MoneyRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ask another user to pay you",
)
@limiter.limit(REQUEST_LIMIT)
def create_request(
    body: MoneyRequestCreate,
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MoneyRequestOut:
    """Creates an invitation. It moves no money and grants no access.

    Only the payer can turn it into a transfer, and only with their PIN.
    """
    req = request_service.create_request(
        db,
        requester=user,
        payer_phone=body.payer_phone,
        amount=body.amount,
        note=body.note,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _to_out(req, user)


@router.get(
    "/requests",
    response_model=MoneyRequestList,
    summary="Requests waiting on you, or ones you sent",
)
def list_requests(
    box: str = Query(default="incoming", pattern="^(incoming|outgoing)$"),
    pending_only: bool = Query(default=False),
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MoneyRequestList:
    rows = request_service.list_requests(
        db, user=user, box=box, include_settled=not pending_only
    )
    return MoneyRequestList(
        requests=[_to_out(r, user) for r in rows],
        pending_incoming=request_service.count_pending_incoming(db, user=user),
    )


@router.post(
    "/requests/{request_id}/approve",
    response_model=MoneyRequestOut,
    summary="Pay a request",
)
@limiter.limit(REQUEST_LIMIT)
def approve_request(
    body: RespondToRequest,
    request: Request,
    response: Response,
    request_id: UUID = Path(description="From GET /api/requests"),
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MoneyRequestOut:
    """Settles through the ordinary transfer path — same locks, same ledger.

    The transfer and the status change commit together, so there is no moment
    where the money has moved but the request still looks payable.
    """
    req, _txn = request_service.approve(
        db,
        payer=user,
        request_id=request_id,
        pin=body.pin,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _to_out(req, user)


@router.post(
    "/requests/{request_id}/decline",
    response_model=MoneyRequestOut,
    summary="Decline a request addressed to you",
)
def decline_request(
    request: Request,
    request_id: UUID = Path(...),
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MoneyRequestOut:
    req = request_service.decline(
        db,
        payer=user,
        request_id=request_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _to_out(req, user)


@router.post(
    "/requests/{request_id}/cancel",
    response_model=MoneyRequestOut,
    summary="Withdraw a request you sent",
)
def cancel_request(
    request: Request,
    request_id: UUID = Path(...),
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MoneyRequestOut:
    req = request_service.cancel(
        db,
        requester=user,
        request_id=request_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _to_out(req, user)
