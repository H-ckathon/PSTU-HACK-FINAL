"""Adversarial input.

The existing suite proves the system does what it claims. This one tries to
break it. The standard applied throughout: **a malformed request must produce a
4xx, never a 5xx.** A 500 means the input reached somewhere it should not have,
and in a money system that is a defect regardless of whether money moved.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.conftest import balance_of, ledger_sum


def send(client, headers, *, to, amount, pin="8317", key=None, note=None):
    return client.post(
        "/api/transfers",
        json={"recipient_phone": to, "amount": amount, "pin": pin, "note": note},
        headers={**headers, **({"Idempotency-Key": key} if key else {})},
    )


# --- amounts --------------------------------------------------------------


def test_smallest_possible_transfer(client, db, alice, bob, auth_headers):
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="0.01")
    assert r.status_code == 201
    assert balance_of(db, alice.id) == Decimal("99999.99")
    assert ledger_sum(db) == Decimal("0.00")


def test_draining_the_wallet_to_exactly_zero(client, db, alice, bob, auth_headers):
    """The boundary the CHECK constraint sits on."""
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="100000.00")
    assert r.status_code == 201
    assert balance_of(db, alice.id) == Decimal("0.00")

    # And one poisha more is refused.
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="0.01")
    assert r.status_code == 422 and r.json()["code"] == "insufficient_funds"


@pytest.mark.parametrize(
    "amount",
    [
        "NaN",              # Decimal accepts this; every comparison is False
        "Infinity",         # Decimal accepts this too, and it IS > 0
        "-Infinity",
        "1e3",              # scientific notation for 1000
        "1E-2",
        "0.001",            # more precision than money has
        "0.00",
        "-0.00",
        "-1",
        "",
        " ",
        "1,000.00",         # thousands separator
        "٣٠٠",              # Arabic-Indic digits
        "10000000000000000.00",   # beyond NUMERIC(15,2)
        None,
        True,
        [],
        {"$gt": 0},
    ],
)
def test_hostile_amounts_are_refused_without_a_500(client, alice, bob, auth_headers, amount):
    r = client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": amount, "pin": "8317"},
        headers=auth_headers(alice.phone),
    )
    assert 400 <= r.status_code < 500, f"{amount!r} produced {r.status_code}"


def test_hostile_amounts_move_no_money(client, db, alice, bob, auth_headers):
    for amount in ["NaN", "Infinity", "1e3", "-1", "0.001"]:
        client.post(
            "/api/transfers",
            json={"recipient_phone": bob.phone, "amount": amount, "pin": "8317"},
            headers=auth_headers(alice.phone),
        )
    assert balance_of(db, alice.id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


# --- text fields ----------------------------------------------------------


def test_note_at_the_length_limit(client, alice, bob, auth_headers):
    h = auth_headers(alice.phone)
    assert send(client, h, to=bob.phone, amount="1.00", note="x" * 255).status_code == 201
    assert send(client, h, to=bob.phone, amount="1.00", note="x" * 256).status_code == 422


def test_note_with_a_null_byte(client, alice, bob, auth_headers):
    """PostgreSQL text cannot contain U+0000, and psycopg2 raises on it.

    An unhandled ValueError here would be a 500 on a money endpoint.
    """
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00",
             note="before\x00after")
    assert 400 <= r.status_code < 500, f"null byte produced {r.status_code}"


def test_note_with_unicode_and_emoji(client, alice, bob, auth_headers):
    note = "দুপুরের খাবার 🍛 — thanks!"
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00", note=note)
    assert r.status_code == 201
    assert r.json()["note"] == note


def test_note_with_html_is_returned_as_text(client, alice, bob, auth_headers):
    payload = "<script>alert('x')</script>"
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00", note=payload)
    assert r.status_code == 201
    assert r.json()["note"] == payload


def test_whitespace_only_note_becomes_null(client, alice, bob, auth_headers):
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00", note="   \t  ")
    assert r.status_code == 201
    assert r.json()["note"] is None


# --- phone numbers --------------------------------------------------------


@pytest.mark.parametrize(
    "phone",
    [
        " 01722222222",     # leading space
        "01722222222 ",     # trailing space
        "+8801722222222",   # international form
        "0172222222",       # too short
        "017222222222",     # too long
        "01022222222",      # invalid operator prefix
        "01A22222222",
        "",
        None,
    ],
)
def test_malformed_recipient_numbers(client, alice, bob, auth_headers, phone):
    r = client.post(
        "/api/transfers",
        json={"recipient_phone": phone, "amount": "1.00", "pin": "8317"},
        headers=auth_headers(alice.phone),
    )
    assert 400 <= r.status_code < 500, f"{phone!r} produced {r.status_code}"


def test_transfer_to_a_deactivated_account(client, db, alice, bob, auth_headers):
    db.execute(text("UPDATE users SET is_active = FALSE WHERE id = :i"), {"i": str(bob.id)})
    db.commit()
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00")
    assert r.status_code == 404


# --- idempotency keys -----------------------------------------------------


def test_oversized_idempotency_key(client, alice, bob, auth_headers):
    """The column is VARCHAR(64). A longer header must not reach it raw."""
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00", key="k" * 300)
    assert 400 <= r.status_code < 500, f"long key produced {r.status_code}"


def test_idempotency_key_at_exactly_the_limit(client, alice, bob, auth_headers):
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00", key="k" * 64)
    assert r.status_code == 201


def test_idempotency_key_with_odd_characters(client, alice, bob, auth_headers):
    r = send(client, auth_headers(alice.phone), to=bob.phone, amount="1.00",
             key="key-with-<>&'\"-chars")
    assert 200 <= r.status_code < 500


def test_same_key_different_recipient_is_a_conflict(client, alice, bob, mallory, auth_headers):
    h = auth_headers(alice.phone)
    key = "one-key-two-destinations"
    assert send(client, h, to=bob.phone, amount="10.00", key=key).status_code == 201
    r = send(client, h, to=mallory.phone, amount="10.00", key=key)
    assert r.status_code == 409 and r.json()["code"] == "idempotency_key_conflict"


# --- tokens ---------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic YWxpY2U6cGFzcw==",
        "bearer ",
        "Bearer a.b.c",
        "Bearer " + "x" * 5000,
        "Bearer null",
        "Token abc",
    ],
)
def test_malformed_authorization_headers(client, header):
    r = client.get("/api/me", headers={"Authorization": header})
    assert r.status_code == 401, f"{header[:24]!r} produced {r.status_code}"


def test_token_missing_required_claims(client, alice):
    import jwt

    from app.config import settings

    for payload in [
        {"exp": 9999999999},                                  # no sub, no sid
        {"sub": str(alice.id), "exp": 9999999999},            # no sid
        {"sub": str(alice.id), "sid": str(alice.id), "exp": 9999999999, "typ": "refresh"},
        {"sub": "not-a-uuid", "sid": str(alice.id), "exp": 9999999999, "typ": "access"},
    ]:
        token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
        assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_expired_token_is_rejected(client, alice):
    import jwt

    from app.config import settings

    token = jwt.encode(
        {"sub": str(alice.id), "sid": str(alice.id), "exp": 1000000000, "typ": "access"},
        settings.secret_key,
        algorithm="HS256",
    )
    r = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "expired" in r.json()["message"].lower()


def test_double_logout_is_harmless(client, alice, auth_headers):
    h = auth_headers(alice.phone)
    assert client.post("/api/auth/logout", headers=h).status_code == 204
    assert client.post("/api/auth/logout", headers=h).status_code == 401


# --- pagination -----------------------------------------------------------


@pytest.mark.parametrize("cursor", ["", "garbage", "!!!!", "e30=", "x" * 500, "../../etc/passwd"])
def test_tampered_cursor_falls_back_to_page_one(client, alice, bob, auth_headers, cursor):
    """A cursor is a position hint, not a security boundary."""
    h = auth_headers(alice.phone)
    send(client, h, to=bob.phone, amount="1.00")
    r = client.get("/api/wallet/statement", params={"cursor": cursor}, headers=h)
    assert r.status_code == 200
    assert len(r.json()["entries"]) >= 1


@pytest.mark.parametrize("limit,expected", [(0, 422), (1, 200), (100, 200), (101, 422), (-5, 422)])
def test_statement_limit_bounds(client, alice, auth_headers, limit, expected):
    r = client.get("/api/wallet/statement", params={"limit": limit}, headers=auth_headers(alice.phone))
    assert r.status_code == expected


def test_reference_lookup_is_not_guessable(client, alice, auth_headers):
    h = auth_headers(alice.phone)
    for ref in ["TXN00000000", "%", "' OR 1=1 --", "TXN" + "X" * 40]:
        assert client.get(f"/api/transfers/{ref}", headers=h).status_code in (404, 422)


# --- registration ---------------------------------------------------------


def test_name_is_normalised_not_rejected(client):
    r = client.post(
        "/api/auth/register",
        json={
            "phone": "01766666666",
            "full_name": "  Rafiqul   Islam  ",
            "password": "a-good-password",
            "pin": "9271",
        },
    )
    assert r.status_code == 201
    assert r.json()["user"]["full_name"] == "Rafiqul Islam"


@pytest.mark.parametrize(
    "password",
    ["short", "", " " * 20, "x" * 73],  # too short, empty, blank, past bcrypt's 72-byte limit
)
def test_bad_passwords_are_refused(client, password):
    r = client.post(
        "/api/auth/register",
        json={"phone": "01755555555", "full_name": "Test User", "password": password, "pin": "8261"},
    )
    assert 400 <= r.status_code < 500


def test_a_72_byte_password_is_accepted_and_verifiable(client):
    """bcrypt truncates past 72 bytes. The cap is why that can never bite us."""
    password = "p" * 72
    assert client.post(
        "/api/auth/register",
        json={"phone": "01744444444", "full_name": "Long Pass", "password": password, "pin": "3947"},
    ).status_code == 201
    assert client.post(
        "/api/auth/login", json={"phone": "01744444444", "password": password}
    ).status_code == 200
    # And a different password that shares the first 72 bytes must NOT work.
    assert client.post(
        "/api/auth/login", json={"phone": "01744444444", "password": password + "extra"}
    ).status_code in (401, 422)


def test_multibyte_name_survives_the_round_trip(client):
    r = client.post(
        "/api/auth/register",
        json={
            "phone": "01733333333",
            "full_name": "মোহাম্মদ রফিকুল ইসলাম",
            "password": "another-good-one",
            "pin": "5813",
        },
    )
    assert r.status_code == 201
    assert r.json()["user"]["full_name"] == "মোহাম্মদ রফিকুল ইসলাম"


# --- money requests -------------------------------------------------------


def test_request_larger_than_the_payers_balance_is_allowed_to_exist(
    client, db, alice, bob, auth_headers
):
    """Asking is not taking. The refusal belongs at approval time, not here."""
    r = client.post(
        "/api/requests",
        json={"payer_phone": alice.phone, "amount": "500000.00"},
        headers=auth_headers(bob.phone),
    )
    assert r.status_code == 201
    assert balance_of(db, alice.id) == Decimal("100000.00")


def test_request_id_that_is_not_a_uuid(client, alice, auth_headers):
    r = client.post(
        "/api/requests/not-a-uuid/approve", json={"pin": "8317"}, headers=auth_headers(alice.phone)
    )
    assert r.status_code == 422


def test_unknown_request_id_is_a_404(client, alice, auth_headers):
    r = client.post(
        "/api/requests/00000000-0000-0000-0000-000000000099/approve",
        json={"pin": "8317"},
        headers=auth_headers(alice.phone),
    )
    assert r.status_code == 404


@pytest.mark.parametrize("pin", ["", "12", "12345", "abcd", "83 7", None, 8317])
def test_malformed_pins(client, alice, bob, auth_headers, pin):
    r = client.post(
        "/api/transfers",
        json={"recipient_phone": bob.phone, "amount": "1.00", "pin": pin},
        headers=auth_headers(alice.phone),
    )
    assert 400 <= r.status_code < 500, f"{pin!r} produced {r.status_code}"


# --- request body itself --------------------------------------------------


def test_malformed_json_body(client, alice, auth_headers):
    r = client.post(
        "/api/transfers",
        content=b"{not json at all",
        headers={**auth_headers(alice.phone), "Content-Type": "application/json"},
    )
    assert 400 <= r.status_code < 500


def test_empty_body(client, alice, auth_headers):
    r = client.post("/api/transfers", json={}, headers=auth_headers(alice.phone))
    assert r.status_code == 422


def test_deeply_nested_body_is_rejected(client, alice, auth_headers):
    nested = {"recipient_phone": {"a": {"b": {"c": [1, 2, {"d": "e"}]}}}, "amount": "1.00", "pin": "8317"}
    r = client.post("/api/transfers", json=nested, headers=auth_headers(alice.phone))
    assert r.status_code == 422


def test_extra_fields_are_refused_loudly(client, alice, bob, auth_headers):
    r = client.post(
        "/api/transfers",
        json={
            "recipient_phone": bob.phone,
            "amount": "1.00",
            "pin": "8317",
            "status": "COMPLETED",
            "reference": "TXNFORGED01",
        },
        headers=auth_headers(alice.phone),
    )
    assert r.status_code == 422
