"""The test that matters.

Everything else in this suite checks that the code does what it says. This
module checks that the DATABASE does what we claim, under real simultaneous
load, with real row locks.

Run it in front of the judges:

    pytest tests/test_concurrency.py -v
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.errors import InsufficientFunds
from app.database import SessionLocal
from app.models import User
from app.services import transfer_service
from tests.conftest import balance_of, ledger_sum

pytestmark = pytest.mark.concurrency


def _transfer_in_own_session(sender_id, recipient_id, amount: str, idem_key=None):
    """Each thread gets its own Session — SQLAlchemy sessions are not shared.

    This is what makes the test real: N threads, N connections, N genuinely
    concurrent transactions competing for the same row lock in PostgreSQL.
    """
    db = SessionLocal()
    try:
        sender = db.get(User, sender_id)
        recipient = db.get(User, recipient_id)
        transfer_service.execute_transfer(
            db,
            sender=sender,
            recipient=recipient,
            amount=Decimal(amount),
            idempotency_key=idem_key,
        )
        return True
    except InsufficientFunds:
        return False
    finally:
        db.close()


def test_no_double_spend_under_concurrency(db, alice, bob):
    """Alice holds 100,000. Twenty threads each try to send 10,000.

    Exactly ten can succeed. If the balance were read before the lock — the
    classic lost-update bug — more than ten would pass and Alice would end up
    overdrawn, or the wallet balance would disagree with the ledger.
    """
    assert balance_of(db, alice.id) == Decimal("100000.00")
    alice_id, bob_id = alice.id, bob.id

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(lambda _: _transfer_in_own_session(alice_id, bob_id, "10000.00"), range(20))
        )

    assert sum(results) == 10, f"expected exactly 10 successes, got {sum(results)}"
    assert balance_of(db, alice_id) == Decimal("0.00")
    assert balance_of(db, bob_id) == Decimal("200000.00")

    # Invariant 01 survived twenty simultaneous writers.
    assert ledger_sum(db) == Decimal("0.00")

    # Invariant 03: the projection still agrees with the ledger.
    drifted = db.execute(
        text(
            "SELECT COUNT(*) FROM ("
            "  SELECT w.id FROM wallets w LEFT JOIN ledger_entries e ON e.wallet_id = w.id"
            "  GROUP BY w.id, w.balance"
            "  HAVING w.balance <> COALESCE(SUM(e.amount), 0)) x"
        )
    ).scalar_one()
    assert drifted == 0


def test_bidirectional_transfers_do_not_deadlock(db, alice, bob):
    """A->B and B->A at the same instant.

    With naive locking each transaction would hold the lock the other needs and
    PostgreSQL would kill one with a deadlock error. Sorting wallet ids before
    acquiring locks means every transaction in the system takes them in the
    same global order, so a wait cycle cannot form.

    A deadlock here would surface as an exception, not a wrong number — which
    is why this test asserts on completion, not just on balances.
    """
    alice_id, bob_id = alice.id, bob.id
    start = threading.Barrier(2)

    def a_to_b():
        start.wait()
        return [_transfer_in_own_session(alice_id, bob_id, "100.00") for _ in range(25)]

    def b_to_a():
        start.wait()
        return [_transfer_in_own_session(bob_id, alice_id, "100.00") for _ in range(25)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1, f2 = pool.submit(a_to_b), pool.submit(b_to_a)
        r1, r2 = f1.result(timeout=60), f2.result(timeout=60)

    assert all(r1) and all(r2), "a transfer failed — check for deadlock in the log"

    # 25 each way at the same amount: both back where they started.
    assert balance_of(db, alice_id) == Decimal("100000.00")
    assert balance_of(db, bob_id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_concurrent_retries_of_one_key_move_money_once(db, alice, bob):
    """Ten threads, one idempotency key — the double-tapped Send button.

    Nine lose the race on the partial unique index. All ten still return the
    winner's transaction, and the money moves exactly once.
    """
    alice_id, bob_id = alice.id, bob.id
    key = "same-key-for-every-thread"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(
            pool.map(
                lambda _: _transfer_in_own_session(alice_id, bob_id, "5000.00", key),
                range(10),
            )
        )

    assert all(results)
    assert balance_of(db, alice_id) == Decimal("95000.00")
    assert balance_of(db, bob_id) == Decimal("105000.00")

    moved = db.execute(
        text("SELECT COUNT(*) FROM transactions WHERE idempotency_key = :k"), {"k": key}
    ).scalar_one()
    assert moved == 1, f"{moved} transactions written for one idempotency key"
    assert ledger_sum(db) == Decimal("0.00")
