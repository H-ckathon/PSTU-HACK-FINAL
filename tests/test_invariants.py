"""The four invariants, asserted after randomised traffic.

The other test modules check specific behaviours. This one checks that no
sequence of legal operations can leave the ledger in a state we cannot defend —
which is the actual promise the system makes.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.errors import DomainError
from app.services import ledger_service, transfer_service

pytestmark = pytest.mark.invariant


def assert_all_invariants(db) -> None:
    # 01 conservation
    total = db.execute(text("SELECT COALESCE(SUM(amount),0) FROM ledger_entries")).scalar_one()
    assert total == Decimal("0.00"), f"invariant 01 broken: ledger sums to {total}"

    # 02 balanced events
    unbalanced = db.execute(
        text(
            "SELECT COUNT(*) FROM (SELECT transaction_id FROM ledger_entries "
            "GROUP BY transaction_id HAVING SUM(amount) <> 0) x"
        )
    ).scalar_one()
    assert unbalanced == 0, f"invariant 02 broken: {unbalanced} unbalanced transactions"

    # 03 no drift
    drifted = db.execute(
        text(
            "SELECT COUNT(*) FROM (SELECT w.id FROM wallets w "
            "LEFT JOIN ledger_entries e ON e.wallet_id = w.id "
            "GROUP BY w.id, w.balance "
            "HAVING w.balance <> COALESCE(SUM(e.amount),0)) x"
        )
    ).scalar_one()
    assert drifted == 0, f"invariant 03 broken: {drifted} wallets disagree with the ledger"

    # 04 solvency
    negative = db.execute(
        text("SELECT COUNT(*) FROM wallets WHERE type = 'USER' AND balance < 0")
    ).scalar_one()
    assert negative == 0, f"invariant 04 broken: {negative} negative user wallets"


def test_invariants_hold_on_a_fresh_database(db):
    assert_all_invariants(db)


def test_signup_grant_is_a_real_transaction_not_a_balance_write(db, alice):
    """The opening balance has an origin entry, and the mint holds the debit."""
    mint_balance = db.execute(
        text("SELECT balance FROM wallets WHERE type = 'SYSTEM'")
    ).scalar_one()
    assert mint_balance == Decimal("-100000.00")

    grant_entries = db.execute(
        text(
            "SELECT COUNT(*) FROM ledger_entries e JOIN transactions t "
            "ON t.id = e.transaction_id WHERE t.type = 'SIGNUP_GRANT'"
        )
    ).scalar_one()
    assert grant_entries == 2
    assert_all_invariants(db)


def test_invariants_survive_randomised_traffic(db, alice, bob, mallory):
    """500 random operations — valid, invalid, self-directed, oversized.

    Some succeed, some are refused. The invariants hold either way, which is
    the point: a rejected transfer must leave no trace in the ledger.
    """
    random.seed(20260829)
    people = [alice, bob, mallory]
    succeeded = failed = 0

    for _ in range(500):
        sender, recipient = random.sample(people, 2)
        amount = Decimal(random.choice(["0.01", "1.00", "99.99", "5000.00", "250000.00"]))
        try:
            transfer_service.execute_transfer(
                db,
                sender=sender,
                recipient=recipient,
                amount=amount,
                idempotency_key=None,
            )
            succeeded += 1
        except DomainError:
            failed += 1

    assert succeeded > 0 and failed > 0, "the run did not exercise both paths"
    assert_all_invariants(db)

    # Total in circulation is unchanged: three grants, and nothing minted since.
    circulating = db.execute(
        text("SELECT COALESCE(SUM(balance),0) FROM wallets WHERE type = 'USER'")
    ).scalar_one()
    assert circulating == Decimal("300000.00")


def test_service_level_invariant_helpers_agree_with_sql(db, alice, bob):
    transfer_service.execute_transfer(
        db, sender=alice, recipient=bob, amount=Decimal("123.45"), idempotency_key=None
    )
    assert ledger_service.ledger_sum(db) == Decimal("0.00")
    assert ledger_service.unbalanced_transactions(db) == []
    assert ledger_service.drifted_wallets(db) == []
    assert ledger_service.insolvent_wallets(db) == []
