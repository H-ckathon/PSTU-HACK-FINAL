"""Refunds — the correction path.

The README claims, under deliberate omissions:

    No soft deletes on financial rows. A correction is a REVERSAL transaction;
    history is never rewritten.

This module is that sentence, implemented. A refund does not touch the original
transaction's entries, does not delete anything, and does not adjust a balance
by hand. It posts a **new** transaction of type `REVERSAL` with its own pair of
signed entries, pointing back at the one it corrects — so the ledger holds both
the mistake and the fix, and `SUM(entries) = 0` survives untouched.

Who may refund: the side that RECEIVED the money, and only them. A refund moves
money out of the refunder's wallet, so it is a spend, and it is authorised the
way every other spend is — with the PIN. The original sender cannot "claw back"
a payment, because that would be exactly the pull-from-someone-else's-wallet
capability the whole system is built to make impossible.

Two independent defences against refunding twice, mirroring money requests:

  1. The original transaction row is locked FOR UPDATE for the whole operation,
     so a second attempt waits and then finds it already REVERSED.
  2. A partial unique index (`uq_one_reversal_per_transaction`) means the
     database itself permits at most one reversal per transaction — and the
     settlement carries the deterministic key `reversal:<id>` as well.

And it is one transaction: the reversal, its entries, both balance updates and
the status change on the original commit together or not at all.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core import security
from app.core.errors import (
    InvalidPin,
    NotRefundable,
    RefundNotAllowed,
    TransactionNotFound,
)
from app.models import AuditAction, Transaction, TxnStatus, TxnType, User, Wallet
from app.services import transfer_service
from app.services.auth_service import audit

# A signup grant has no counterparty to return money to, and a reversal cannot
# itself be reversed — that way lies an unbounded chain of corrections.
REFUNDABLE_TYPES = {TxnType.TRANSFER, TxnType.REQUEST_SETTLEMENT}


def refund(
    db: DbSession,
    *,
    actor: User,
    reference: str,
    pin: str,
    reason: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[Transaction, Transaction]:
    """Return money you received. Returns (reversal, original)."""
    if actor.wallet is None:
        raise TransactionNotFound(reference)

    # Scoped by wallet in the WHERE clause, and locked for the whole operation.
    # Someone else's reference and a reference that never existed produce the
    # same 404, so references cannot be probed.
    original = db.execute(
        select(Transaction)
        .where(
            Transaction.reference == reference,
            (Transaction.sender_wallet_id == actor.wallet.id)
            | (Transaction.receiver_wallet_id == actor.wallet.id),
        )
        .with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()

    if original is None:
        raise TransactionNotFound(reference)

    # Only the recipient. The sender cannot pull money back out of someone
    # else's wallet — that capability must not exist anywhere in this system.
    if original.receiver_wallet_id != actor.wallet.id:
        raise RefundNotAllowed()

    if original.type not in REFUNDABLE_TYPES:
        raise NotRefundable(
            "Only a transfer or a settled request can be returned."
        )
    if original.status == TxnStatus.REVERSED:
        raise NotRefundable("This transfer has already been returned.")
    if original.status != TxnStatus.COMPLETED:
        raise NotRefundable(
            f"This transfer is {original.status.value.lower()} and cannot be returned."
        )

    # Refunding spends money, so it is authorised like any other spend.
    if not security.verify_secret(pin, actor.pin_hash):
        audit(
            db,
            AuditAction.PIN_FAILED,
            actor=actor.id,
            entity_type="transaction",
            entity_id=original.id,
            ip=ip,
            user_agent=user_agent,
        )
        db.commit()
        raise InvalidPin()

    # Whoever sent it originally gets it back — resolved from the wallet on the
    # original transaction, never from anything the caller supplied.
    payee = db.execute(
        select(User).join(Wallet, Wallet.user_id == User.id).where(
            Wallet.id == original.sender_wallet_id
        )
    ).scalar_one_or_none()
    if payee is None or not payee.is_active:
        raise NotRefundable("The original sender's account is no longer active.")

    # The ordinary money path, in the opposite direction. commit=False so the
    # reversal and the status change on the original land in one atomic unit.
    reversal, _replay = transfer_service.execute_transfer(
        db,
        sender=actor,
        recipient=payee,
        amount=original.amount,
        idempotency_key=f"reversal:{original.id}",
        note=reason or f"Refund of {original.reference}",
        txn_type=TxnType.REVERSAL,
        reverses_transaction_id=original.id,
        ip=ip,
        user_agent=user_agent,
        commit=False,
    )

    original.status = TxnStatus.REVERSED

    audit(
        db,
        AuditAction.REFUND_ISSUED,
        actor=actor.id,
        entity_type="transaction",
        entity_id=reversal.id,
        ip=ip,
        user_agent=user_agent,
        amount=str(original.amount),
        reverses=original.reference,
        reference=reversal.reference,
    )
    db.commit()
    db.refresh(reversal)
    db.refresh(original)
    return reversal, original


def is_refundable(txn: Transaction, viewer_wallet_id) -> bool:
    """Used by the statement, so the interface offers the action only where it works."""
    return (
        txn.receiver_wallet_id == viewer_wallet_id
        and txn.type in REFUNDABLE_TYPES
        and txn.status == TxnStatus.COMPLETED
    )
