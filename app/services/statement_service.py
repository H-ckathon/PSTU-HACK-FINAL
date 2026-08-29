"""Statement reads.

Keyset pagination, not OFFSET. `OFFSET 500000` makes PostgreSQL walk half a
million rows it will then throw away; a keyset predicate stays an index seek at
any depth. The ordering pair `(created_at DESC, id DESC)` matches the
`idx_entries_wallet` index exactly, and `id` breaks ties so no row is ever
skipped or shown twice when two entries share a timestamp — which they always
do, because both sides of a transfer are written in the same instant.

This is a scalability claim implemented rather than asserted.
"""

from __future__ import annotations

import base64
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session as DbSession

from app.core.errors import TransactionNotFound
from app.models import LedgerEntry, Transaction, User, Wallet
from app.services import refund_service

MAX_PAGE = 100


def encode_cursor(created_at: datetime, entry_id: int) -> str:
    raw = f"{created_at.isoformat()}|{entry_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        ts, entry_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(ts), int(entry_id)
    except (ValueError, TypeError):
        # A malformed cursor shows page one rather than a 500. It is a
        # position hint, not a security boundary.
        return None


def _counterparty_names(db: DbSession, wallet_ids: set[UUID]) -> dict[UUID, User]:
    """One extra query for the whole page, instead of one per row."""
    if not wallet_ids:
        return {}
    rows = db.execute(
        select(Wallet.id, User).join(User, User.id == Wallet.user_id).where(
            Wallet.id.in_(wallet_ids)
        )
    ).all()
    return {wallet_id: user for wallet_id, user in rows}


def statement_page(
    db: DbSession,
    *,
    wallet_id: UUID,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:
    """Returns (rows, next_cursor). Each row is ready for StatementEntry."""
    limit = max(1, min(limit, MAX_PAGE))

    stmt = (
        select(LedgerEntry, Transaction)
        .join(Transaction, Transaction.id == LedgerEntry.transaction_id)
        .where(LedgerEntry.wallet_id == wallet_id)
        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        .limit(limit + 1)  # one extra row tells us whether another page exists
    )

    if cursor:
        position = decode_cursor(cursor)
        if position is not None:
            stmt = stmt.where(
                tuple_(LedgerEntry.created_at, LedgerEntry.id) < position
            )

    pairs = db.execute(stmt).all()

    has_more = len(pairs) > limit
    pairs = pairs[:limit]

    # Resolve the other side of each entry: whichever wallet on the
    # transaction is not this one.
    other_wallet_ids: set[UUID] = set()
    for entry, txn in pairs:
        other = (
            txn.receiver_wallet_id
            if entry.wallet_id == txn.sender_wallet_id
            else txn.sender_wallet_id
        )
        if other is not None:
            other_wallet_ids.add(other)

    names = _counterparty_names(db, other_wallet_ids)

    rows: list[dict] = []
    for entry, txn in pairs:
        other = (
            txn.receiver_wallet_id
            if entry.wallet_id == txn.sender_wallet_id
            else txn.sender_wallet_id
        )
        party = names.get(other) if other else None
        rows.append(
            {
                "id": entry.id,
                "reference": txn.reference,
                "type": txn.type.value if hasattr(txn.type, "value") else str(txn.type),
                "direction": entry.direction,
                "amount": entry.amount,
                "balance_after": entry.balance_after,
                # No counterparty means the system mint — shown as such rather
                # than as a mystery blank.
                "counterparty": (
                    {"phone": party.phone, "full_name": party.full_name}
                    if party
                    else None
                ),
                "note": txn.note,
                "created_at": entry.created_at,
                "status": txn.status.value if hasattr(txn.status, "value") else str(txn.status),
                # Computed here so the interface can offer Refund only where it
                # would actually succeed, rather than showing a button that 403s.
                "refundable": refund_service.is_refundable(txn, wallet_id),
            }
        )

    next_cursor = (
        encode_cursor(pairs[-1][0].created_at, pairs[-1][0].id) if has_more and pairs else None
    )
    return rows, next_cursor


def get_transaction_for_user(db: DbSession, *, reference: str, wallet_id: UUID) -> Transaction:
    """Fetch one transaction, but only if this wallet was a party to it.

    Scoping the query by wallet rather than filtering after the fetch means an
    unrelated reference and a non-existent reference are indistinguishable from
    outside.
    """
    txn = db.execute(
        select(Transaction).where(
            Transaction.reference == reference,
            (Transaction.sender_wallet_id == wallet_id)
            | (Transaction.receiver_wallet_id == wallet_id),
        )
    ).scalar_one_or_none()
    if txn is None:
        raise TransactionNotFound(reference)
    return txn
