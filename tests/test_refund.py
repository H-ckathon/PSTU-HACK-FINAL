"""Refunds.

The claim being tested: *a correction is a new transaction, never an edit.*
Every test here checks the ledger as well as the balances, because the point of
the feature is what it does to the history, not only to the numbers.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.models import User
from app.services import refund_service
from tests.conftest import balance_of, ledger_sum


def send(client, headers, *, to, amount, pin="8317", note=None):
    return client.post(
        "/api/transfers",
        json={"recipient_phone": to, "amount": amount, "pin": pin, "note": note},
        headers=headers,
    )


def refund(client, headers, reference, pin, reason=None):
    return client.post(
        f"/api/transfers/{reference}/refund",
        json={"pin": pin, "reason": reason},
        headers=headers,
    )


# --- the happy path -------------------------------------------------------


def test_refund_returns_the_money_and_writes_new_entries(client, db, alice, bob, auth_headers):
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    original = send(client, a, to=bob.phone, amount="2500.00", note="Wrong person").json()

    r = refund(client, b, original["reference"], "4629", reason="Sent to me by mistake")
    assert r.status_code == 201, r.text
    reversal = r.json()

    assert reversal["type"] == "REVERSAL"
    assert reversal["reverses_reference"] == original["reference"]
    assert reversal["recipient"]["full_name"] == "Alice Rahman"

    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert balance_of(db, bob.id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_the_original_entries_are_untouched(client, db, alice, bob, auth_headers):
    """The correction is additive. Nothing is edited, nothing disappears."""
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    original = send(client, a, to=bob.phone, amount="1000.00").json()

    before = db.execute(
        text("SELECT id, amount, balance_after FROM ledger_entries ORDER BY id")
    ).all()
    refund(client, b, original["reference"], "4629")
    after = db.execute(
        text("SELECT id, amount, balance_after FROM ledger_entries ORDER BY id")
    ).all()

    # Every pre-existing row survives, byte for byte, and two new ones appear.
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2

    # The original is marked REVERSED but its entries still say what happened.
    status = db.execute(
        text("SELECT status FROM transactions WHERE reference = :r"),
        {"r": original["reference"]},
    ).scalar_one()
    assert status == "REVERSED"


def test_both_sides_see_the_correction_in_their_statement(client, alice, bob, auth_headers):
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    original = send(client, a, to=bob.phone, amount="750.00").json()
    refund(client, b, original["reference"], "4629")

    alice_entries = client.get("/api/wallet/statement", headers=a).json()["entries"]
    assert alice_entries[0]["type"] == "REVERSAL"
    assert Decimal(alice_entries[0]["amount"]) == Decimal("750.00")     # money back in
    assert Decimal(alice_entries[1]["amount"]) == Decimal("-750.00")    # the original out
    assert alice_entries[1]["status"] == "REVERSED"

    bob_entries = client.get("/api/wallet/statement", headers=b).json()["entries"]
    assert Decimal(bob_entries[0]["amount"]) == Decimal("-750.00")


def test_a_settled_request_can_also_be_refunded(client, db, alice, bob, auth_headers):
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    req = client.post(
        "/api/requests", json={"payer_phone": alice.phone, "amount": "600.00"}, headers=b
    ).json()
    settled = client.post(
        f"/api/requests/{req['id']}/approve", json={"pin": "8317"}, headers=a
    ).json()

    # Bob received it, so Bob is the one who can give it back.
    r = refund(client, b, settled["transaction_reference"], "4629")
    assert r.status_code == 201
    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


# --- who may refund -------------------------------------------------------


@pytest.mark.security
def test_the_sender_cannot_claw_money_back(client, db, alice, bob, auth_headers):
    """The core security claim.

    If the sender could reverse a transfer unilaterally, this system would
    contain a way to take money out of someone else's wallet — the exact
    capability every other design decision exists to prevent.
    """
    a = auth_headers(alice.phone)
    original = send(client, a, to=bob.phone, amount="5000.00").json()

    r = refund(client, a, original["reference"], "8317")
    assert r.status_code == 403
    assert r.json()["code"] == "refund_not_allowed"
    assert balance_of(db, bob.id) == Decimal("105000.00")


@pytest.mark.security
def test_a_stranger_gets_the_same_answer_as_for_a_missing_reference(
    client, alice, bob, mallory, auth_headers
):
    original = send(client, auth_headers(alice.phone), to=bob.phone, amount="100.00").json()
    m = auth_headers(mallory.phone)
    assert refund(client, m, original["reference"], "6274").status_code == 404
    assert refund(client, m, "TXNDOESNOTEX", "6274").status_code == 404


def test_refund_needs_the_refunders_pin(client, db, alice, bob, auth_headers):
    original = send(client, auth_headers(alice.phone), to=bob.phone, amount="100.00").json()
    r = refund(client, auth_headers(bob.phone), original["reference"], "0000")
    assert r.status_code == 403 and r.json()["code"] == "invalid_pin"
    assert balance_of(db, bob.id) == Decimal("100100.00")


def test_refund_requires_authentication(client, alice, bob, auth_headers):
    original = send(client, auth_headers(alice.phone), to=bob.phone, amount="100.00").json()
    assert client.post(
        f"/api/transfers/{original['reference']}/refund", json={"pin": "4629"}
    ).status_code == 401


# --- what cannot be refunded ---------------------------------------------


def test_a_signup_grant_cannot_be_refunded(client, db, alice, auth_headers):
    """There is no counterparty to return it to."""
    grant = db.execute(
        text("SELECT reference FROM transactions WHERE type = 'SIGNUP_GRANT' LIMIT 1")
    ).scalar_one()
    r = refund(client, auth_headers(alice.phone), grant, "8317")
    assert r.status_code == 409 and r.json()["code"] == "not_refundable"


def test_a_refund_cannot_itself_be_refunded(client, alice, bob, auth_headers):
    """Otherwise corrections chain without bound."""
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    original = send(client, a, to=bob.phone, amount="200.00").json()
    reversal = refund(client, b, original["reference"], "4629").json()

    r = refund(client, a, reversal["reference"], "8317")
    assert r.status_code == 409 and r.json()["code"] == "not_refundable"


def test_refunding_twice_is_refused(client, db, alice, bob, auth_headers):
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    original = send(client, a, to=bob.phone, amount="300.00").json()

    assert refund(client, b, original["reference"], "4629").status_code == 201
    second = refund(client, b, original["reference"], "4629")
    assert second.status_code == 409 and second.json()["code"] == "not_refundable"

    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert balance_of(db, bob.id) == Decimal("100000.00")


def test_refund_fails_cleanly_when_the_money_is_already_spent(
    client, db, alice, bob, mallory, auth_headers
):
    """Bob received 5,000, spent everything, and now cannot return it.

    The refusal must leave the original refundable rather than consuming it —
    the reversal and the status change share one transaction, so an
    insufficient-funds rollback undoes both.
    """
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    original = send(client, a, to=bob.phone, amount="5000.00").json()
    send(client, b, to=mallory.phone, amount="105000.00", pin="4629")
    assert balance_of(db, bob.id) == Decimal("0.00")

    r = refund(client, b, original["reference"], "4629")
    assert r.status_code == 422 and r.json()["code"] == "insufficient_funds"

    status = db.execute(
        text("SELECT status FROM transactions WHERE reference = :r"),
        {"r": original["reference"]},
    ).scalar_one()
    assert status == "COMPLETED", "a failed refund consumed the original"
    assert ledger_sum(db) == Decimal("0.00")


def test_statement_marks_only_what_you_can_actually_refund(client, alice, bob, auth_headers):
    """The interface offers the action only where it would succeed."""
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    send(client, a, to=bob.phone, amount="100.00")

    alice_entries = client.get("/api/wallet/statement", headers=a).json()["entries"]
    # Alice sent it and received her grant from the mint: neither is refundable.
    assert [e["refundable"] for e in alice_entries] == [False, False]

    bob_entries = client.get("/api/wallet/statement", headers=b).json()["entries"]
    assert bob_entries[0]["refundable"] is True    # the transfer he received
    assert bob_entries[1]["refundable"] is False   # his signup grant


# --- concurrency ----------------------------------------------------------


@pytest.mark.concurrency
def test_simultaneous_refunds_return_the_money_once(client, db, alice, bob, auth_headers):
    """Eight threads refund the same transfer at the same instant.

    Row lock, unique index on `reverses_transaction_id`, and the deterministic
    idempotency key are three independent reasons this can only happen once.
    """
    original = send(client, auth_headers(alice.phone), to=bob.phone, amount="1000.00").json()
    bob_id, reference = bob.id, original["reference"]

    def once(_):
        session = SessionLocal()
        try:
            refund_service.refund(
                session, actor=session.get(User, bob_id), reference=reference, pin="4629"
            )
            return "ok"
        except Exception as exc:
            return type(exc).__name__
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(once, range(8)))

    assert results.count("ok") == 1, f"{results.count('ok')} refunds succeeded: {results}"
    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert balance_of(db, bob.id) == Decimal("100000.00")

    reversals = db.execute(
        text("SELECT COUNT(*) FROM transactions WHERE type = 'REVERSAL'")
    ).scalar_one()
    assert reversals == 1
    assert ledger_sum(db) == Decimal("0.00")


@pytest.mark.invariant
def test_invariants_hold_after_a_refund(client, db, alice, bob, auth_headers):
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)
    original = send(client, a, to=bob.phone, amount="4321.00").json()
    refund(client, b, original["reference"], "4629")

    report = client.get("/api/admin/reconcile", headers=a).json()
    assert report["all_hold"] is True
    assert Decimal(report["ledger_sum"]) == Decimal("0.00")
    # Two grants, one transfer, one reversal: eight entries.
    assert report["entry_count"] == 8
