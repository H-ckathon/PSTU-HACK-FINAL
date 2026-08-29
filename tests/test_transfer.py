"""Transfer behaviour, over HTTP."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from tests.conftest import balance_of, ledger_sum


def send(client, headers, *, to, amount, pin, key=None, note=None):
    return client.post(
        "/api/transfers",
        json={"recipient_phone": to, "amount": amount, "pin": pin, "note": note},
        headers={**headers, **({"Idempotency-Key": key} if key else {})},
    )


# --- the happy path -------------------------------------------------------


def test_transfer_moves_money_and_writes_two_entries(client, db, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    r = send(client, h, to=bob.phone, amount="2500.00", pin="8317", note="Lunch")
    assert r.status_code == 201, r.text

    body = r.json()
    assert body["status"] == "COMPLETED"
    assert body["reference"].startswith("TXN")
    assert body["recipient"]["full_name"] == "Bob Karim"
    assert Decimal(body["balance_after"]) == Decimal("97500.00")

    assert balance_of(db, alice.id) == Decimal("97500.00")
    assert balance_of(db, bob.id) == Decimal("102500.00")

    entries = db.execute(
        text(
            "SELECT e.direction, e.amount FROM ledger_entries e "
            "JOIN transactions t ON t.id = e.transaction_id "
            "WHERE t.reference = :r ORDER BY e.amount"
        ),
        {"r": body["reference"]},
    ).all()
    assert [(d, a) for d, a in entries] == [
        ("DEBIT", Decimal("-2500.00")),
        ("CREDIT", Decimal("2500.00")),
    ]
    assert ledger_sum(db) == Decimal("0.00")


def test_statement_shows_signed_entries_and_running_balance(client, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    send(client, h, to=bob.phone, amount="1000.00", pin="8317")
    send(client, h, to=bob.phone, amount="250.50", pin="8317")

    r = client.get("/api/wallet/statement", headers=h)
    assert r.status_code == 200
    page = r.json()

    assert Decimal(page["balance"]) == Decimal("98749.50")
    # newest first: 250.50 out, 1000 out, then the signup grant in
    assert [e["direction"] for e in page["entries"]] == ["DEBIT", "DEBIT", "CREDIT"]
    assert Decimal(page["entries"][0]["amount"]) == Decimal("-250.50")
    assert Decimal(page["entries"][0]["balance_after"]) == Decimal("98749.50")
    assert page["entries"][0]["counterparty"]["full_name"] == "Bob Karim"
    # The opening balance has an origin: a SIGNUP_GRANT from the system mint.
    assert page["entries"][-1]["type"] == "SIGNUP_GRANT"


def test_statement_pagination_is_stable(client, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    for _ in range(7):
        send(client, h, to=bob.phone, amount="10.00", pin="8317")

    seen, cursor, pages = [], None, 0
    while True:
        r = client.get(
            "/api/wallet/statement",
            params={"limit": 3, **({"cursor": cursor} if cursor else {})},
            headers=h,
        )
        page = r.json()
        seen.extend(e["id"] for e in page["entries"])
        pages += 1
        cursor = page["next_cursor"]
        if not cursor or pages > 10:
            break

    # 7 transfers + 1 signup grant, each appearing exactly once
    assert len(seen) == 8
    assert len(set(seen)) == 8


# --- idempotency ----------------------------------------------------------


def test_same_key_replays_instead_of_resending(client, db, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    key = str(uuid4())

    first = send(client, h, to=bob.phone, amount="500.00", pin="8317", key=key)
    second = send(client, h, to=bob.phone, amount="500.00", pin="8317", key=key)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["reference"] == second.json()["reference"]
    assert first.json()["idempotent_replay"] is False
    assert second.json()["idempotent_replay"] is True
    assert balance_of(db, alice.id) == Decimal("99500.00")


def test_reused_key_with_different_amount_is_rejected(client, alice, bob, auth_headers):
    """Replay is only safe when the request is identical.

    Silently returning the original for a DIFFERENT request would be worse than
    the double-send it prevents.
    """
    h = auth_headers(alice.phone)
    key = str(uuid4())
    send(client, h, to=bob.phone, amount="500.00", pin="8317", key=key)

    r = send(client, h, to=bob.phone, amount="900.00", pin="8317", key=key)
    assert r.status_code == 409
    assert r.json()["code"] == "idempotency_key_conflict"


def test_keys_are_scoped_per_user(client, db, alice, bob, mallory, auth_headers):
    """Alice's key does not collide with Mallory's identical key."""
    key = "shared-string"
    send(client, auth_headers(alice.phone), to=bob.phone, amount="100.00", pin="8317", key=key)
    r = send(client, auth_headers(mallory.phone), to=bob.phone, amount="100.00", pin="6274", key=key)
    assert r.status_code == 201
    assert r.json()["idempotent_replay"] is False
    assert balance_of(db, bob.id) == Decimal("100200.00")


# --- refusals -------------------------------------------------------------


def test_overdraft_is_refused(client, db, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    r = send(client, h, to=bob.phone, amount="100000.01", pin="8317")
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_funds"
    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_wrong_pin_is_refused(client, db, alice, bob, auth_headers):
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="10.00", pin="0000")
    assert r.status_code == 403
    assert r.json()["code"] == "invalid_pin"
    assert balance_of(db, alice.id) == Decimal("100000.00")


def test_self_transfer_is_refused(client, alice, auth_headers):
    r = send(client, auth_headers(alice.phone), to=alice.phone, amount="10.00", pin="8317")
    assert r.status_code == 422
    assert r.json()["code"] == "self_transfer_not_allowed"


def test_unknown_recipient_is_refused(client, alice, auth_headers):
    r = send(client, auth_headers(alice.phone), to="01755555555", amount="10.00", pin="8317")
    assert r.status_code == 404


def test_bad_amounts_are_refused_at_the_schema(client, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    for amount in ["0", "-100.00", "10.999", "abc"]:
        r = send(client, h, to=bob.phone, amount=amount, pin="8317")
        assert r.status_code == 422, f"{amount} was accepted"


def test_float_precision_is_not_lost(client, db, alice, bob, auth_headers):
    """0.1 + 0.2 in floats is not 0.3. In NUMERIC it is.

    Three transfers of 0.10 leave exactly 99999.70 — no drifting cents.
    """
    h = auth_headers(alice.phone)
    for _ in range(3):
        send(client, h, to=bob.phone, amount="0.10", pin="8317")
    assert balance_of(db, alice.id) == Decimal("99999.70")
    assert balance_of(db, bob.id) == Decimal("100000.30")
