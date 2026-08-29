"""Rate limiting and the reconciliation endpoint.

Rate limits are the outer ring of the abuse defence. Per-account login lockout
sits behind them, so an attacker rotating IP addresses still cannot brute force
one account — the two mechanisms fail differently on purpose.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.conftest import PASSWORD

pytestmark = pytest.mark.security


# --- rate limiting --------------------------------------------------------


def test_login_attempts_are_throttled(client, limits_on, alice):
    """Six wrong passwords in a minute, and BOTH defences fire in order.

    Attempts 1-4  ->  401, ordinary rejection
    Attempt  5    ->  423, the per-account lockout engages
    Attempt  6    ->  429, the per-key rate limit refuses to even reach bcrypt

    The precedence is the right way round: locking the account is the stronger
    and more specific response, and it survives long after the rate-limit
    window has rolled over.
    """
    codes = [
        client.post(
            "/api/auth/login", json={"phone": alice.phone, "password": "wrong-password"}
        ).status_code
        for _ in range(6)
    ]
    assert codes[:4] == [401] * 4
    assert codes[4] == 423, "account lockout did not engage on the fifth failure"
    assert codes[5] == 429, "rate limit did not engage on the sixth attempt"


def test_throttled_response_is_helpful(client, limits_on, alice):
    for _ in range(6):
        r = client.post("/api/auth/login", json={"phone": alice.phone, "password": "nope-nope"})
    assert r.status_code == 429
    body = r.json()
    assert body["code"] == "rate_limited"
    assert "Retry-After" in r.headers
    # Same error envelope as every other failure, so one client handler covers all.
    assert set(body) == {"code", "message", "details"}


def test_transfers_are_limited_per_account_not_per_ip(client, limits_on, alice, bob, auth_headers):
    """Two users behind one IP must not throttle each other.

    An IP-only limit would be wrong here: a whole office or campus can share a
    single NAT address, so one busy user would lock out everyone around them.
    """
    a, b = auth_headers(alice.phone), auth_headers(bob.phone)

    codes = []
    for _ in range(11):
        codes.append(
            client.post(
                "/api/transfers",
                json={"recipient_phone": bob.phone, "amount": "1.00", "pin": "8317"},
                headers=a,
            ).status_code
        )
    assert codes.count(429) >= 1, "alice was never throttled"

    # Bob, from the same IP, is unaffected.
    r = client.post(
        "/api/transfers",
        json={"recipient_phone": alice.phone, "amount": "1.00", "pin": "4629"},
        headers=b,
    )
    assert r.status_code == 201


def test_lookup_enumeration_is_bounded(client, limits_on, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    codes = [
        client.get("/api/users/lookup", params={"phone": bob.phone}, headers=h).status_code
        for _ in range(22)
    ]
    assert 429 in codes


def test_lockout_and_rate_limit_are_independent(client, limits_on, db, alice):
    """The account lock outlives the rate-limit window.

    Rate limiting is per-key and expires in a minute; lockout is per-account
    and lasts fifteen. Clearing one does not clear the other.
    """
    for _ in range(5):
        client.post("/api/auth/login", json={"phone": alice.phone, "password": "wrong-password"})

    locked = db.execute(
        text("SELECT locked_until FROM users WHERE id = :i"), {"i": str(alice.id)}
    ).scalar_one()
    assert locked is not None

    from app.core.limiter import reset_limits

    reset_limits()  # pretend a minute passed
    r = client.post("/api/auth/login", json={"phone": alice.phone, "password": PASSWORD})
    assert r.status_code == 423, "correct password succeeded while the account was locked"


def test_limits_do_not_apply_to_reads(client, limits_on, alice, auth_headers):
    """Checking your own balance is not abuse. Only writes and auth are limited."""
    h = auth_headers(alice.phone)
    assert all(client.get("/api/me", headers=h).status_code == 200 for _ in range(30))


# --- reconciliation -------------------------------------------------------


def test_reconcile_reports_all_four_invariants(client, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": "4321.00", "pin": "8317"},
        headers=h,
    )

    r = client.get("/api/admin/reconcile", headers=h)
    assert r.status_code == 200
    report = r.json()

    assert report["conservation"] is True
    assert report["balanced_events"] is True
    assert report["no_drift"] is True
    assert report["solvency"] is True
    assert report["all_hold"] is True
    assert Decimal(report["ledger_sum"]) == Decimal("0.00")
    assert report["entry_count"] == 6  # two signup grants, one transfer
    assert report["offending"] == {}


def test_reconcile_detects_a_planted_inconsistency(client, db, alice, bob, auth_headers):
    """Proof the check is real and not a hardcoded green light.

    We tamper with a wallet balance directly — which the ledger trigger cannot
    stop, because `wallets` is a projection, not the ledger — and reconcile
    catches the drift and names the wallet.
    """
    h = auth_headers(alice.phone)
    assert client.get("/api/admin/reconcile", headers=h).json()["all_hold"] is True

    db.execute(
        text("UPDATE wallets SET balance = balance + 1 WHERE user_id = :u"),
        {"u": str(alice.id)},
    )
    db.commit()

    report = client.get("/api/admin/reconcile", headers=h).json()
    assert report["no_drift"] is False
    assert report["all_hold"] is False
    assert str(alice.wallet.id) in report["offending"]["drifted_wallets"]
    # The ledger itself is untouched: only the projection drifted.
    assert report["conservation"] is True


def test_reconcile_requires_authentication(client):
    assert client.get("/api/admin/reconcile").status_code == 401


# --- audit trail ----------------------------------------------------------


def test_activity_shows_your_own_events_only(client, alice, bob, auth_headers):
    a = auth_headers(alice.phone)
    client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": "10.00", "pin": "8317"},
        headers=a,
    )
    client.post("/api/auth/login", json={"phone": alice.phone, "password": "wrong-password"})

    events = client.get("/api/me/activity", headers=a).json()["events"]
    actions = {e["action"] for e in events}
    assert "TRANSFER_COMPLETED" in actions
    assert "LOGIN_FAILED" in actions
    assert "REGISTERED" in actions

    # Bob's trail is his own, and does not contain Alice's login failure.
    bob_actions = {e["action"] for e in client.get("/api/me/activity", headers=auth_headers(bob.phone)).json()["events"]}
    assert "LOGIN_FAILED" not in bob_actions
