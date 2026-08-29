"""Money requests: the flow, and everything that must not work.

The security claim is one sentence — *a request is an invitation, never an
authorization* — and these tests are what make it a claim rather than a slogan.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.models import User
from app.services import request_service
from tests.conftest import balance_of, ledger_sum


def ask(client, headers, *, payer, amount, note=None):
    return client.post(
        "/api/requests",
        json={"payer_phone": payer, "amount": amount, "note": note},
        headers=headers,
    )


# --- the flow -------------------------------------------------------------


def test_request_then_approve_moves_money(client, db, alice, bob, auth_headers):
    """Bob asks Alice for 1,200. Alice approves. Money moves once."""
    bob_h, alice_h = auth_headers(bob.phone), auth_headers(alice.phone)

    r = ask(client, bob_h, payer=alice.phone, amount="1200.00", note="Dinner")
    assert r.status_code == 201
    req = r.json()
    assert req["status"] == "PENDING"
    assert req["direction"] == "OUTGOING"
    assert req["counterparty"]["full_name"] == "Alice Rahman"

    # Creating the request moved nothing.
    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert balance_of(db, bob.id) == Decimal("100000.00")

    # It appears in Alice's incoming box.
    incoming = client.get("/api/requests", headers=alice_h).json()
    assert incoming["pending_incoming"] == 1
    assert incoming["requests"][0]["direction"] == "INCOMING"

    r = client.post(
        f"/api/requests/{req['id']}/approve", json={"pin": "8317"}, headers=alice_h
    )
    assert r.status_code == 200, r.text
    settled = r.json()
    assert settled["status"] == "APPROVED"
    assert settled["transaction_reference"].startswith("TXN")

    assert balance_of(db, alice.id) == Decimal("98800.00")
    assert balance_of(db, bob.id) == Decimal("101200.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_settlement_uses_the_ordinary_ledger_path(client, db, alice, bob, auth_headers):
    """Same guarantees as a direct send: two signed entries, typed correctly."""
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="500.00").json()
    client.post(
        f"/api/requests/{req['id']}/approve",
        json={"pin": "8317"},
        headers=auth_headers(alice.phone),
    )

    rows = db.execute(
        text(
            "SELECT t.type, e.direction, e.amount FROM ledger_entries e "
            "JOIN transactions t ON t.id = e.transaction_id "
            "WHERE t.type = 'REQUEST_SETTLEMENT' ORDER BY e.amount"
        )
    ).all()
    assert [(t, d, a) for t, d, a in rows] == [
        ("REQUEST_SETTLEMENT", "DEBIT", Decimal("-500.00")),
        ("REQUEST_SETTLEMENT", "CREDIT", Decimal("500.00")),
    ]


def test_decline_and_cancel_move_nothing(client, db, alice, bob, auth_headers):
    bob_h, alice_h = auth_headers(bob.phone), auth_headers(alice.phone)

    declined = ask(client, bob_h, payer=alice.phone, amount="100.00").json()
    r = client.post(f"/api/requests/{declined['id']}/decline", headers=alice_h)
    assert r.status_code == 200 and r.json()["status"] == "DECLINED"

    cancelled = ask(client, bob_h, payer=alice.phone, amount="100.00").json()
    r = client.post(f"/api/requests/{cancelled['id']}/cancel", headers=bob_h)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"

    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert balance_of(db, bob.id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


# --- a request is an invitation, never an authorization -------------------


@pytest.mark.security
def test_requester_cannot_approve_their_own_request(client, db, alice, bob, auth_headers):
    """The whole security model in one test.

    If the requester could approve, a request would be a way to take money from
    someone else's wallet — which is precisely what it must never be.
    """
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="50000.00").json()

    r = client.post(
        f"/api/requests/{req['id']}/approve",
        json={"pin": "4629"},  # Bob's own PIN
        headers=auth_headers(bob.phone),
    )
    assert r.status_code == 404
    assert balance_of(db, alice.id) == Decimal("100000.00")


@pytest.mark.security
def test_approval_requires_the_payers_pin(client, db, alice, bob, auth_headers):
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="100.00").json()
    r = client.post(
        f"/api/requests/{req['id']}/approve",
        json={"pin": "0000"},
        headers=auth_headers(alice.phone),
    )
    assert r.status_code == 403 and r.json()["code"] == "invalid_pin"
    assert balance_of(db, alice.id) == Decimal("100000.00")


@pytest.mark.security
def test_a_stranger_cannot_see_or_touch_a_request(client, alice, bob, mallory, auth_headers):
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="100.00").json()
    m = auth_headers(mallory.phone)

    for path in ("approve", "decline", "cancel"):
        payload = {"json": {"pin": "6274"}} if path == "approve" else {}
        r = client.post(f"/api/requests/{req['id']}/{path}", headers=m, **payload)
        assert r.status_code == 404, f"{path} answered {r.status_code}"

    assert client.get("/api/requests", headers=m).json()["requests"] == []


def test_payer_cannot_cancel_and_requester_cannot_decline(client, alice, bob, auth_headers):
    """Each side gets exactly the verb that belongs to it."""
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="100.00").json()
    assert client.post(f"/api/requests/{req['id']}/cancel", headers=auth_headers(alice.phone)).status_code == 404
    assert client.post(f"/api/requests/{req['id']}/decline", headers=auth_headers(bob.phone)).status_code == 404


# --- no double payment ----------------------------------------------------


def test_a_request_cannot_be_paid_twice(client, db, alice, bob, auth_headers):
    alice_h = auth_headers(alice.phone)
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="700.00").json()

    first = client.post(f"/api/requests/{req['id']}/approve", json={"pin": "8317"}, headers=alice_h)
    second = client.post(f"/api/requests/{req['id']}/approve", json={"pin": "8317"}, headers=alice_h)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "request_not_pending"
    assert balance_of(db, alice.id) == Decimal("99300.00")


@pytest.mark.concurrency
def test_simultaneous_approvals_pay_once(client, db, alice, bob, auth_headers):
    """Eight threads approve the same request at the same instant.

    Two independent defences: the request row is locked FOR UPDATE, and the
    settlement carries the deterministic key `request:<id>`. Money moves once.
    """
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="1000.00").json()
    request_id, alice_id = req["id"], alice.id

    def approve_once(_):
        session = SessionLocal()
        try:
            payer = session.get(User, alice_id)
            request_service.approve(
                session, payer=payer, request_id=UUID(request_id), pin="8317"
            )
            return True
        except Exception:
            return False
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(approve_once, range(8)))

    assert sum(results) == 1, f"{sum(results)} approvals succeeded"
    assert balance_of(db, alice.id) == Decimal("99000.00")
    assert balance_of(db, bob.id) == Decimal("101000.00")

    settlements = db.execute(
        text("SELECT COUNT(*) FROM transactions WHERE type = 'REQUEST_SETTLEMENT'")
    ).scalar_one()
    assert settlements == 1
    assert ledger_sum(db) == Decimal("0.00")


# --- validation and lifecycle --------------------------------------------


def test_cannot_request_from_yourself(client, alice, auth_headers):
    r = ask(client, auth_headers(alice.phone), payer=alice.phone, amount="10.00")
    assert r.status_code == 422 and r.json()["code"] == "self_request_not_allowed"


def test_unknown_payer_is_rejected(client, bob, auth_headers):
    assert ask(client, auth_headers(bob.phone), payer="01755555555", amount="10.00").status_code == 404


def test_bad_amounts_are_rejected(client, alice, bob, auth_headers):
    h = auth_headers(bob.phone)
    for amount in ["0", "-5.00", "1.234"]:
        assert ask(client, h, payer=alice.phone, amount=amount).status_code == 422


def test_expired_request_cannot_be_approved(client, db, alice, bob, auth_headers):
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="100.00").json()
    db.execute(
        text("UPDATE money_requests SET expires_at = :t WHERE id = :i"),
        {"t": datetime.now(UTC) - timedelta(hours=1), "i": req["id"]},
    )
    db.commit()

    listed = client.get("/api/requests", headers=auth_headers(alice.phone)).json()
    assert listed["requests"][0]["status"] == "EXPIRED"
    assert listed["pending_incoming"] == 0

    r = client.post(
        f"/api/requests/{req['id']}/approve", json={"pin": "8317"}, headers=auth_headers(alice.phone)
    )
    assert r.status_code == 410 and r.json()["code"] == "request_expired"
    assert balance_of(db, alice.id) == Decimal("100000.00")


def test_approving_more_than_you_hold_is_refused_and_leaves_it_pending(
    client, db, alice, bob, auth_headers
):
    """A failed settlement must not consume the request.

    The transfer and the status change share one transaction, so an
    insufficient-funds rollback leaves the request payable later.
    """
    req = ask(client, auth_headers(bob.phone), payer=alice.phone, amount="150000.00").json()
    r = client.post(
        f"/api/requests/{req['id']}/approve", json={"pin": "8317"}, headers=auth_headers(alice.phone)
    )
    assert r.status_code == 422 and r.json()["code"] == "insufficient_funds"

    still = client.get("/api/requests", headers=auth_headers(alice.phone)).json()
    assert still["requests"][0]["status"] == "PENDING"
    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")
