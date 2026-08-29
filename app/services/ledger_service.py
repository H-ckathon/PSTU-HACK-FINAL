"""The ledger — the only module in the system that writes money.

Two primitives, and every money movement in the application is built from them:

    lock_wallets(...)     acquire row locks in a deterministic order
    post_double_entry(...)  append two signed entries that sum to zero

Keeping this in one place means the correctness argument is auditable in about
eighty lines, and every caller inherits the same guarantees.

CONTRACT: `post_double_entry` assumes its wallets are already locked by
`lock_wallets` inside the caller's transaction. It never commits — the caller
owns the transaction boundary, because the caller is the one who knows what
else belongs in the same atomic unit.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import InsufficientFunds, WalletMissing
from app.core.security import make_reference
from app.models import LedgerEntry, Transaction, TxnStatus, TxnType, Wallet


def lock_wallets(db: Session, *wallet_ids: UUID) -> dict[UUID, Wallet]:
    """Lock the given wallets FOR UPDATE, in ascending id order.

    The ordering is the entire deadlock story. If A→B locked (A, B) while B→A
    locked (B, A), two concurrent transfers would each hold the lock the other
    needs. Sorting means every transaction in the system acquires locks in the
    same global order, so a cycle cannot form and deadlock is structurally
    impossible — not merely unlikely.
    """
    unique_ids = sorted(set(wallet_ids))
    if not unique_ids:
        return {}

    rows = db.execute(
        select(Wallet)
        .where(Wallet.id.in_(unique_ids))
        .order_by(Wallet.id)
        .with_for_update(),
        # populate_existing is NOT optional here, and it is subtle enough to be
        # worth spelling out. If a Wallet is already in this Session's identity
        # map — and it is, because `user.wallet` is eagerly joined — SQLAlchemy
        # returns the cached Python object and DISCARDS the freshly selected
        # row. PostgreSQL would take the lock correctly and we would then read
        # a stale balance from memory: the exact lost-update bug this function
        # exists to prevent, hidden one layer up.
        #
        # `tests/test_concurrency.py::test_no_double_spend_under_concurrency`
        # fails without this line: 20 of 20 transfers succeed against a wallet
        # that can fund 10.
        execution_options={"populate_existing": True},
    ).scalars().all()

    wallets = {w.id: w for w in rows}
    missing = set(unique_ids) - set(wallets)
    if missing:
        raise WalletMissing()
    return wallets


def post_double_entry(
    db: Session,
    *,
    txn_type: TxnType,
    amount: Decimal,
    debit_wallet: Wallet,
    credit_wallet: Wallet,
    initiated_by: UUID | None = None,
    note: str | None = None,
    idempotency_key: str | None = None,
    allow_overdraft: bool = False,
    reverses_transaction_id: UUID | None = None,
) -> Transaction:
    """Append one balanced business event. Does not commit.

    `allow_overdraft` is True only for the SYSTEM mint, which goes negative by
    design so that every taka in the closed ecosystem has a provable origin.
    User wallets never pass it, and the database CHECK constraint is the
    backstop if a caller ever gets that wrong.
    """
    if amount <= 0:
        raise ValueError("Ledger amounts must be positive; direction comes from the wallets.")

    if not allow_overdraft and debit_wallet.balance < amount:
        raise InsufficientFunds(available=debit_wallet.balance)

    txn = Transaction(
        reference=make_reference(),
        type=txn_type,
        status=TxnStatus.PENDING,
        amount=amount,
        sender_wallet_id=debit_wallet.id,
        receiver_wallet_id=credit_wallet.id,
        note=note,
        idempotency_key=idempotency_key,
        initiated_by=initiated_by,
        reverses_transaction_id=reverses_transaction_id,
    )
    db.add(txn)
    db.flush()  # assigns txn.id without ending the transaction

    # Update the projection first so balance_after is accurate on each entry.
    debit_wallet.balance = debit_wallet.balance - amount
    credit_wallet.balance = credit_wallet.balance + amount

    db.add_all(
        [
            LedgerEntry(
                transaction_id=txn.id,
                wallet_id=debit_wallet.id,
                amount=-amount,               # signed: negative is a debit
                balance_after=debit_wallet.balance,
            ),
            LedgerEntry(
                transaction_id=txn.id,
                wallet_id=credit_wallet.id,
                amount=amount,                # signed: positive is a credit
                balance_after=credit_wallet.balance,
            ),
        ]
    )

    txn.status = TxnStatus.COMPLETED
    txn.completed_at = func.now()
    db.flush()
    return txn


# --- reconciliation -------------------------------------------------------


def ledger_sum(db: Session) -> Decimal:
    """Invariant 01. Must be exactly zero, always."""
    return db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0))
    ).scalar_one()


def unbalanced_transactions(db: Session) -> list[UUID]:
    """Invariant 02. Must be empty."""
    rows = db.execute(
        select(LedgerEntry.transaction_id)
        .group_by(LedgerEntry.transaction_id)
        .having(func.sum(LedgerEntry.amount) != 0)
    ).scalars().all()
    return list(rows)


def drifted_wallets(db: Session) -> list[UUID]:
    """Invariant 03. The projection must equal the ledger, for every wallet."""
    rows = db.execute(
        select(Wallet.id)
        .outerjoin(LedgerEntry, LedgerEntry.wallet_id == Wallet.id)
        .group_by(Wallet.id, Wallet.balance)
        .having(Wallet.balance != func.coalesce(func.sum(LedgerEntry.amount), 0))
    ).scalars().all()
    return list(rows)


def insolvent_wallets(db: Session) -> list[UUID]:
    """Invariant 04. No USER wallet may be negative."""
    rows = db.execute(
        select(Wallet.id).where(Wallet.type == "USER", Wallet.balance < 0)
    ).scalars().all()
    return list(rows)
