"""Money requests — the collect-money flow.

The brief asks for it directly:

    "My friend owes me 1,200. I want to collect it through the application."

The security shape matters more than the feature. A request is an INVITATION,
never an AUTHORIZATION: it moves no money, it grants nothing, and it cannot
pull from anyone. Only the payer can turn it into a transfer, and only by
re-entering their PIN. The requester never touches the payer's wallet.

Approval reuses `transfer_service.execute_transfer` unchanged, so a settled
request gets exactly the same guarantees as a direct send — ordered locks,
lock-then-read, the overdraft constraint, an immutable pair of ledger entries.

Two independent defences against paying a request twice:

  1. The request row is locked FOR UPDATE for the whole approval, so a second
     approval waits and then finds the status is no longer PENDING.
  2. The settlement carries a deterministic idempotency key, `request:<id>`.
     Even if step 1 were somehow bypassed, the partial unique index means the
     second attempt returns the first transaction rather than moving money.

And the whole approval — transfer, ledger entries, status change, audit — is
ONE database transaction, so there is no window in which the money has moved
but the request still looks payable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.core import security
from app.core.errors import (
    InvalidPin,
    RecipientNotFound,
    RequestExpired,
    RequestNotFound,
    RequestNotPending,
    SelfRequestNotAllowed,
)
from app.models import (
    AuditAction,
    MoneyRequest,
    RequestStatus,
    Transaction,
    TxnType,
    User,
)
from app.services import transfer_service
from app.services.auth_service import audit


def _now() -> datetime:
    return datetime.now(UTC)


def effective_status(req: MoneyRequest) -> RequestStatus:
    """A pending request past its expiry reads as EXPIRED.

    Computed rather than swept by a background job: there is no scheduler in
    this system, and a request that looks pending but cannot be paid would be
    a lie to the user.
    """
    if req.status == RequestStatus.PENDING and req.expires_at <= _now():
        return RequestStatus.EXPIRED
    return req.status


def _lock_request(db: DbSession, request_id: UUID, *, viewer: User) -> MoneyRequest:
    """Fetch and lock, scoped to someone who is party to it.

    Scoping in the WHERE clause rather than checking afterwards means an
    unknown id and someone else's id are indistinguishable from outside.
    """
    req = db.execute(
        select(MoneyRequest)
        .where(
            MoneyRequest.id == request_id,
            (MoneyRequest.requester_id == viewer.id) | (MoneyRequest.payer_id == viewer.id),
        )
        .with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    if req is None:
        raise RequestNotFound()
    return req


def _assert_actionable(req: MoneyRequest) -> None:
    status = effective_status(req)
    if status == RequestStatus.EXPIRED:
        raise RequestExpired()
    if status != RequestStatus.PENDING:
        raise RequestNotPending(status.value)


# --- create ---------------------------------------------------------------


def create_request(
    db: DbSession,
    *,
    requester: User,
    payer_phone: str,
    amount: Decimal,
    note: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> MoneyRequest:
    payer = db.execute(
        select(User).where(User.phone == payer_phone, User.is_active.is_(True))
    ).scalar_one_or_none()
    if payer is None:
        raise RecipientNotFound(payer_phone)
    if payer.id == requester.id:
        raise SelfRequestNotAllowed()

    req = MoneyRequest(
        requester_id=requester.id,
        payer_id=payer.id,
        amount=amount,
        note=note,
        status=RequestStatus.PENDING,
        expires_at=_now() + timedelta(hours=settings.request_expiry_hours),
    )
    db.add(req)
    db.flush()

    audit(
        db,
        AuditAction.REQUEST_CREATED,
        actor=requester.id,
        entity_type="money_request",
        entity_id=req.id,
        ip=ip,
        user_agent=user_agent,
        amount=str(amount),
        payer=payer.phone,
    )
    db.commit()
    db.refresh(req)
    return req


# --- read -----------------------------------------------------------------


def list_requests(
    db: DbSession,
    *,
    user: User,
    box: str = "incoming",
    include_settled: bool = True,
    limit: int = 50,
) -> list[MoneyRequest]:
    column = MoneyRequest.payer_id if box == "incoming" else MoneyRequest.requester_id
    stmt = select(MoneyRequest).where(column == user.id)
    if not include_settled:
        stmt = stmt.where(MoneyRequest.status == RequestStatus.PENDING)
    stmt = stmt.order_by(MoneyRequest.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def count_pending_incoming(db: DbSession, *, user: User) -> int:
    return db.execute(
        select(func.count(MoneyRequest.id)).where(
            MoneyRequest.payer_id == user.id,
            MoneyRequest.status == RequestStatus.PENDING,
            MoneyRequest.expires_at > _now(),
        )
    ).scalar_one()


# --- act ------------------------------------------------------------------


def approve(
    db: DbSession,
    *,
    payer: User,
    request_id: UUID,
    pin: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[MoneyRequest, Transaction]:
    """Pay a request. One transaction, start to finish."""
    req = _lock_request(db, request_id, viewer=payer)

    # Only the payer may approve. The requester holding the id changes nothing.
    if req.payer_id != payer.id:
        raise RequestNotFound()

    _assert_actionable(req)

    if not security.verify_secret(pin, payer.pin_hash):
        audit(
            db,
            AuditAction.PIN_FAILED,
            actor=payer.id,
            entity_type="money_request",
            entity_id=req.id,
            ip=ip,
            user_agent=user_agent,
        )
        db.commit()
        raise InvalidPin()

    requester = db.get(User, req.requester_id)
    if requester is None or not requester.is_active:
        raise RecipientNotFound("the requester")

    # Same money path as a direct send. commit=False so the transfer and the
    # status change below land in the same atomic unit.
    txn, _replay = transfer_service.execute_transfer(
        db,
        sender=payer,
        recipient=requester,
        amount=req.amount,
        idempotency_key=f"request:{req.id}",
        note=req.note or f"Request from {requester.full_name}",
        txn_type=TxnType.REQUEST_SETTLEMENT,
        ip=ip,
        user_agent=user_agent,
        commit=False,
    )

    req.status = RequestStatus.APPROVED
    req.transaction_id = txn.id
    req.responded_at = func.now()

    audit(
        db,
        AuditAction.REQUEST_APPROVED,
        actor=payer.id,
        entity_type="money_request",
        entity_id=req.id,
        ip=ip,
        user_agent=user_agent,
        amount=str(req.amount),
        reference=txn.reference,
    )
    db.commit()
    db.refresh(req)
    db.refresh(txn)
    return req, txn


def decline(
    db: DbSession,
    *,
    payer: User,
    request_id: UUID,
    ip: str | None = None,
    user_agent: str | None = None,
) -> MoneyRequest:
    req = _lock_request(db, request_id, viewer=payer)
    if req.payer_id != payer.id:
        raise RequestNotFound()
    _assert_actionable(req)

    req.status = RequestStatus.DECLINED
    req.responded_at = func.now()
    audit(
        db,
        AuditAction.REQUEST_DECLINED,
        actor=payer.id,
        entity_type="money_request",
        entity_id=req.id,
        ip=ip,
        user_agent=user_agent,
    )
    db.commit()
    db.refresh(req)
    return req


def cancel(
    db: DbSession,
    *,
    requester: User,
    request_id: UUID,
    ip: str | None = None,
    user_agent: str | None = None,
) -> MoneyRequest:
    """Withdraw a request you sent."""
    req = _lock_request(db, request_id, viewer=requester)
    if req.requester_id != requester.id:
        raise RequestNotFound()
    _assert_actionable(req)

    req.status = RequestStatus.CANCELLED
    req.responded_at = func.now()
    audit(
        db,
        AuditAction.REQUEST_CANCELLED,
        actor=requester.id,
        entity_type="money_request",
        entity_id=req.id,
        ip=ip,
        user_agent=user_agent,
    )
    db.commit()
    db.refresh(req)
    return req
