"""Wallet and statement endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session as DbSession

from app.core.deps import get_current_user
from app.database import get_db
from app.models import User
from app.schemas.transfer import StatementPage
from app.services import statement_service

router = APIRouter(prefix="/api", tags=["wallet"])


@router.get(
    "/wallet/statement",
    response_model=StatementPage,
    summary="Your ledger, newest first",
)
def statement(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(
        default=None, description="From the previous page's next_cursor"
    ),
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StatementPage:
    """The actual ledger entries for your wallet — not a derived summary.

    Signed amounts: negative is money out, positive is money in. Each row also
    carries `balance_after`, so the running balance is history rather than a
    recomputation, and any tampering would be visible as a broken chain.

    Paginated by keyset, so page 5,000 costs the same as page 1.
    """
    rows, next_cursor = statement_service.statement_page(
        db, wallet_id=user.wallet.id, limit=limit, cursor=cursor
    )
    return StatementPage(
        entries=rows, next_cursor=next_cursor, balance=user.wallet.balance
    )
