"""The transfer path.

This is the file to read if you want to understand the system. Everything else
supports it. Five guarantees are enforced here, in this order:

  1. IDEMPOTENCY   a retried request returns the original transaction rather
                   than moving money twice
  2. AUTHORISATION the PIN authorises the ACTION, not merely the session
  3. LOCK ORDER    wallets are locked in ascending id, so deadlock cannot form
  4. LOCK-THEN-READ  the balance is read after the lock, closing the classic
                   lost-update race
  5. ATOMICITY     transaction row, two ledger entries, both balance updates
                   and the audit row are one Postgres transaction

And behind all five, the database's own `no_overdraft` CHECK constraint, which
holds even if every line of this file were wrong.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core import security
from app.core.errors import (
    IdempotencyKeyConflict,
    InsufficientFunds,
    InvalidPin,
    RecipientNotFound,
    SelfTransferNotAllowed,
    WalletMissing,
)
from app.models import AuditAction, Transaction, TxnType, User
from app.services import ledger_service
from app.services.auth_service import audit


def _find_replay(db: DbSession, user_id: UUID, key: str | None) -> Transaction | None:
    if not key:
        return None
    return db.execute(
        select(Transaction).where(
            Transaction.initiated_by == user_id,
            Transaction.idempotency_key == key,
        )
    ).scalar_one_or_none()


def _assert_same_request(txn: Transaction, amount: Decimal, recipient_wallet_id: UUID) -> None:
    """A key may only replay the request it was minted for."""
    if txn.amount != amount or txn.receiver_wallet_id != recipient_wallet_id:
        raise IdempotencyKeyConflict(txn.reference)


def resolve_recipient(db: DbSession, sender: User, phone: str) -> User:
    recipient = db.execute(
        select(User).where(User.phone == phone, User.is_active.is_(True))
    ).scalar_one_or_none()
    if recipient is None:
        raise RecipientNotFound(phone)
    if recipient.id == sender.id:
        raise SelfTransferNotAllowed()
    return recipient


def execute_transfer(
    db: DbSession,
    *,
    sender: User,
    recipient: User,
    amount: Decimal,
    idempotency_key: str | None,
    note: str | None = None,
    txn_type: TxnType = TxnType.TRANSFER,
    ip: str | None = None,
    user_agent: str | None = None,
    commit: bool = True,
) -> tuple[Transaction, bool]:
    """Move money. Returns (transaction, was_idempotent_replay).

    The PIN check happens in `send_money` (or in the request-approval flow),
    not here, so that both entry points share one money path.

    `commit=False` lets a caller fold this into a larger atomic unit — the
    money-request approval does exactly that, so the transfer and the status
    change on the request commit together or not at all. The write section runs
    inside a SAVEPOINT so a constraint violation can be handled without
    destroying the caller's surrounding transaction.
    """
    if sender.wallet is None or recipient.wallet is None:
        raise WalletMissing()
    if recipient.id == sender.id:
        raise SelfTransferNotAllowed()

    # --- 1. idempotency, before anything is locked -----------------------
    replay = _find_replay(db, sender.id, idempotency_key)
    if replay is not None:
        _assert_same_request(replay, amount, recipient.wallet.id)
        return replay, True

    src_id, dst_id = sender.wallet.id, recipient.wallet.id

    try:
        # A SAVEPOINT, so a constraint violation below can be handled without
        # tearing down a transaction the caller may still be building.
        with db.begin_nested():
            # --- 3. deterministic lock order -----------------------------
            # Sorting inside lock_wallets is what makes A->B and B->A safe to
            # run at the same instant: every transaction in the system
            # acquires locks in the same global order, so a wait cycle cannot
            # form.
            wallets = ledger_service.lock_wallets(db, src_id, dst_id)
            src, dst = wallets[src_id], wallets[dst_id]

            # --- 4 & 5. read under the lock, then write it all atomically --
            # post_double_entry re-checks the balance against the LOCKED row
            # and raises InsufficientFunds before writing anything.
            txn = ledger_service.post_double_entry(
                db,
                txn_type=txn_type,
                amount=amount,
                debit_wallet=src,
                credit_wallet=dst,
                initiated_by=sender.id,
                note=note,
                idempotency_key=idempotency_key,
            )

            audit(
                db,
                AuditAction.TRANSFER_COMPLETED,
                actor=sender.id,
                entity_type="transaction",
                entity_id=txn.id,
                ip=ip,
                user_agent=user_agent,
                amount=str(amount),
                reference=txn.reference,
                to=recipient.phone,
            )

    except InsufficientFunds:
        # The savepoint rolled back; nothing was written. Record the refusal.
        audit(
            db,
            AuditAction.TRANSFER_REJECTED,
            actor=sender.id,
            ip=ip,
            user_agent=user_agent,
            amount=str(amount),
            reason="insufficient_funds",
        )
        if commit:
            db.commit()
        raise

    except IntegrityError as exc:
        detail = str(exc.orig)

        # A concurrent duplicate lost the race on the partial unique index.
        # The winner already moved the money; return its transaction.
        if "uq_idem" in detail:
            winner = _find_replay(db, sender.id, idempotency_key)
            if winner is not None:
                _assert_same_request(winner, amount, dst_id)
                return winner, True

        # The database's own backstop fired. If we ever see this, the service
        # layer had a bug and the constraint saved us.
        if "no_overdraft" in detail:
            raise InsufficientFunds() from exc

        raise

    if commit:
        db.commit()
        db.refresh(txn)
    return txn, False


def send_money(
    db: DbSession,
    *,
    sender: User,
    recipient_phone: str,
    amount: Decimal,
    pin: str,
    idempotency_key: str | None,
    note: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[Transaction, bool]:
    """User-facing send: resolve, authorise, then move."""
    recipient = resolve_recipient(db, sender, recipient_phone)

    # --- 2. authorise the ACTION -----------------------------------------
    # A session that has been left open, or a stolen access token, is not
    # enough to move money. The PIN is a second, independent secret.
    if not security.verify_secret(pin, sender.pin_hash):
        audit(
            db,
            AuditAction.PIN_FAILED,
            actor=sender.id,
            ip=ip,
            user_agent=user_agent,
            amount=str(amount),
        )
        db.commit()
        raise InvalidPin()

    return execute_transfer(
        db,
        sender=sender,
        recipient=recipient,
        amount=amount,
        idempotency_key=idempotency_key,
        note=note,
        ip=ip,
        user_agent=user_agent,
    )
