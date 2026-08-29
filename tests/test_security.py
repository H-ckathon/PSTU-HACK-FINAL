"""Attacks that must not work.

Each test is written from the attacker's side: this is the thing someone would
actually try, and here is the response they get.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.conftest import PASSWORD, balance_of

pytestmark = pytest.mark.security


# --- authorization --------------------------------------------------------


def test_cannot_name_someone_elses_wallet_as_the_source(client, alice, mallory, auth_headers):
    """The IDOR / BOLA attack — the most common real API vulnerability.

    There is no `from_wallet_id` field in the schema at all, so this is
    rejected by the contract rather than by a check we might forget to write.
    """
    r = client.post(
        "/api/transfers",
        json={
            "recipient_phone": mallory.phone,
            "amount": "1000.00",
            "pin": "6274",
            "from_wallet_id": str(alice.wallet.id),
        },
        headers=auth_headers(mallory.phone),
    )
    assert r.status_code == 422


def test_cannot_read_someone_elses_transaction(client, db, alice, bob, mallory, auth_headers):
    """A reference is not a capability."""
    r = client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": "100.00", "pin": "8317"},
        headers=auth_headers(alice.phone),
    )
    reference = r.json()["reference"]

    # Both parties can see it.
    assert client.get(f"/api/transfers/{reference}", headers=auth_headers(alice.phone)).status_code == 200
    assert client.get(f"/api/transfers/{reference}", headers=auth_headers(bob.phone)).status_code == 200

    # A stranger gets the same answer as for a reference that never existed.
    outsider = auth_headers(mallory.phone)
    assert client.get(f"/api/transfers/{reference}", headers=outsider).status_code == 404
    assert client.get("/api/transfers/TXNDOESNOTEX", headers=outsider).status_code == 404


def test_statement_only_ever_shows_your_own_wallet(client, alice, bob, auth_headers):
    client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": "777.00", "pin": "8317"},
        headers=auth_headers(alice.phone),
    )
    page = client.get("/api/wallet/statement", headers=auth_headers(bob.phone)).json()
    # Bob sees the credit side only; every entry is his.
    assert all(Decimal(e["amount"]) > 0 for e in page["entries"])
    assert Decimal(page["balance"]) == Decimal("100777.00")


# --- authentication -------------------------------------------------------


def test_protected_endpoints_need_a_token(client):
    for method, path in [
        ("get", "/api/me"),
        ("get", "/api/wallet/statement"),
        ("post", "/api/transfers"),
        ("get", "/api/users/lookup?phone=01711111111"),
    ]:
        r = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 401, f"{path} answered {r.status_code} without a token"


def test_algorithm_confusion_token_is_rejected(client, alice):
    """A forged `alg: none` token — the classic JWT attack.

    `jwt.decode(..., algorithms=["HS256"])` is what closes it. Without that
    whitelist this request would authenticate.
    """
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': str(alice.id), 'sid': str(alice.id), 'exp': 9999999999, 'typ': 'access'})}."
    r = client.get("/api/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_token_signed_with_the_wrong_key_is_rejected(client, alice):
    import jwt

    bad = jwt.encode(
        {"sub": str(alice.id), "sid": str(alice.id), "exp": 9999999999, "typ": "access"},
        "not-the-real-secret",
        algorithm="HS256",
    )
    assert client.get("/api/me", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_login_does_not_leak_which_numbers_are_registered(client, alice):
    unknown = client.post("/api/auth/login", json={"phone": "01700000000", "password": PASSWORD})
    wrong = client.post("/api/auth/login", json={"phone": alice.phone, "password": "wrong-password"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_lookup_never_reveals_a_balance(client, alice, bob, auth_headers):
    r = client.get(
        "/api/users/lookup", params={"phone": bob.phone}, headers=auth_headers(alice.phone)
    )
    assert r.status_code == 200
    assert set(r.json()) == {"phone", "full_name"}


# --- injection and storage ------------------------------------------------


def test_sql_injection_in_a_note_is_stored_as_text(client, db, alice, bob, auth_headers):
    payload = "'; DROP TABLE ledger_entries; --"
    r = client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": "1.00", "pin": "8317", "note": payload},
        headers=auth_headers(alice.phone),
    )
    assert r.status_code == 201
    assert r.json()["note"] == payload
    # The table is still there, with the transfer in it.
    assert db.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar_one() > 0


def test_passwords_and_pins_are_never_stored_or_returned(client, db, alice):
    row = db.execute(
        text("SELECT password_hash, pin_hash FROM users WHERE id = :i"), {"i": str(alice.id)}
    ).one()
    assert PASSWORD not in row[0] and row[0].startswith("$2b$")
    assert "8317" not in row[1] and row[1].startswith("$2b$")
    assert row[0] != row[1]

    r = client.post("/api/auth/login", json={"phone": alice.phone, "password": PASSWORD})
    body = r.text
    assert PASSWORD not in body and "hash" not in body


def test_ledger_cannot_be_edited_even_with_direct_sql(db, alice, bob, client, auth_headers):
    """Append-only is a database guarantee, not a code convention."""
    client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": "50.00", "pin": "8317"},
        headers=auth_headers(alice.phone),
    )
    for statement in (
        "UPDATE ledger_entries SET amount = 999999",
        "DELETE FROM ledger_entries",
    ):
        with pytest.raises(Exception) as err:
            db.execute(text(statement))
            db.commit()
        assert "append-only" in str(err.value)
        db.rollback()

    assert balance_of(db, alice.id) == Decimal("99950.00")
