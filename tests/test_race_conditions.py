"""Races the existing concurrency suite does not cover.

`test_concurrency.py` proves the two classic cases: lost update, and A->B
against B->A. This file goes after the harder ones — cycles longer than two,
two different code paths competing for the same wallet, and lifecycle
operations racing the money that depends on them.

Every test ends by asserting the ledger still sums to zero, because that is the
only claim that has to survive all of them at once.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.core.errors import DomainError
from app.database import SessionLocal
from app.models import MoneyRequest, User
from app.services import request_service, transfer_service
from tests.conftest import balance_of, ledger_sum

pytestmark = pytest.mark.concurrency


def transfer(sender_id, recipient_id, amount, key=None):
    """One transfer in its own Session, as a real concurrent request would be."""
    db = SessionLocal()
    try:
        transfer_service.execute_transfer(
            db,
            sender=db.get(User, sender_id),
            recipient=db.get(User, recipient_id),
            amount=Decimal(amount),
            idempotency_key=key,
        )
        return "ok"
    except DomainError as exc:
        return exc.code
    except Exception as exc:  # a deadlock or serialisation failure lands here
        return f"UNEXPECTED:{type(exc).__name__}:{str(exc)[:120]}"
    finally:
        db.close()


def approve(payer_id, request_id, pin="8317"):
    db = SessionLocal()
    try:
        request_service.approve(
            db, payer=db.get(User, payer_id), request_id=UUID(str(request_id)), pin=pin
        )
        return "ok"
    except DomainError as exc:
        return exc.code
    except Exception as exc:
        return f"UNEXPECTED:{type(exc).__name__}:{str(exc)[:120]}"
    finally:
        db.close()


def decline(payer_id, request_id):
    db = SessionLocal()
    try:
        request_service.decline(db, payer=db.get(User, payer_id), request_id=UUID(str(request_id)))
        return "ok"
    except DomainError as exc:
        return exc.code
    except Exception as exc:
        return f"UNEXPECTED:{type(exc).__name__}:{str(exc)[:120]}"
    finally:
        db.close()


def make_request(db, requester, payer, amount):
    return request_service.create_request(
        db, requester=requester, payer_phone=payer.phone, amount=Decimal(amount)
    )


def assert_no_unexpected(results):
    bad = [r for r in results if isinstance(r, str) and r.startswith("UNEXPECTED")]
    assert not bad, f"unhandled failures: {bad[:3]}"


# --- lock-cycle hunting ---------------------------------------------------


def test_three_way_cycle_does_not_deadlock(db, alice, bob, mallory):
    """A->B, B->C, C->A, all at once.

    Two-party deadlock is the textbook case. A three-node cycle is the one that
    catches implementations which sort only the *pair* rather than every wallet
    the transaction will touch. Sorted acquisition has no length limit, so this
    should complete — and if it did not, PostgreSQL would raise DeadlockDetected
    rather than return a wrong number, which is why the assertion is on
    unhandled exceptions.
    """
    a, b, c = alice.id, bob.id, mallory.id
    gate = threading.Barrier(3)

    def leg(src, dst):
        gate.wait()
        return [transfer(src, dst, "100.00") for _ in range(20)]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(leg, a, b),
            pool.submit(leg, b, c),
            pool.submit(leg, c, a),
        ]
        results = [r for f in futures for r in f.result(timeout=90)]

    assert_no_unexpected(results)
    assert results.count("ok") == 60
    # Round trip: every wallet sent and received 2,000.
    for uid in (a, b, c):
        assert balance_of(db, uid) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_transfer_and_request_approval_compete_for_one_wallet(db, alice, bob, mallory):
    """Two different code paths, same wallet, same instant.

    A direct transfer locks wallets only. An approval locks the request row
    FIRST and then the wallets. Different resource classes acquired in a
    consistent global order (request -> wallets) is what keeps this safe; if
    approval had grabbed wallets before the request row, this test is where it
    would show up.
    """
    reqs = [make_request(db, mallory, alice, "1000.00") for _ in range(5)]
    alice_id, bob_id, mallory_id = alice.id, bob.id, mallory.id
    gate = threading.Barrier(2)

    def drain():
        gate.wait()
        return [transfer(alice_id, bob_id, "1000.00") for _ in range(20)]

    def settle():
        gate.wait()
        return [approve(alice_id, r.id) for r in reqs]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1, f2 = pool.submit(drain), pool.submit(settle)
        results = f1.result(timeout=90) + f2.result(timeout=90)

    assert_no_unexpected(results)
    # 20 transfers out + 5 settlements out = 25,000 leaving Alice.
    assert balance_of(db, alice_id) == Decimal("75000.00")
    assert balance_of(db, bob_id) == Decimal("120000.00")
    assert balance_of(db, mallory_id) == Decimal("105000.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_mutual_request_approval_does_not_deadlock(db, alice, bob):
    """Alice pays Bob's request while Bob pays Alice's, simultaneously."""
    a_asks = make_request(db, alice, bob, "500.00")     # Bob must pay
    b_asks = make_request(db, bob, alice, "500.00")     # Alice must pay
    alice_id, bob_id = alice.id, bob.id
    gate = threading.Barrier(2)

    def alice_pays():
        gate.wait()
        return approve(alice_id, b_asks.id, "8317")

    def bob_pays():
        gate.wait()
        return approve(bob_id, a_asks.id, "4629")

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Both futures must be SUBMITTED before either is awaited: the barrier
        # only releases when two threads reach it, and .result() on the first
        # submission would block the second from ever being scheduled.
        futures = [pool.submit(alice_pays), pool.submit(bob_pays)]
        results = [f.result(timeout=60) for f in futures]

    assert_no_unexpected(results)
    assert results == ["ok", "ok"]
    assert balance_of(db, alice_id) == Decimal("100000.00")
    assert balance_of(db, bob_id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


# --- exhausting a wallet from several directions --------------------------

def test_drain_from_two_directions_never_overdraws(db, alice, bob, mallory):
    """Alice sends to Bob and to Mallory at once, for more than she holds.

    Twenty threads at 10,000 across two destinations; only ten can be funded.
    """
    a, b, c = alice.id, bob.id, mallory.id

    def one(i):
        return transfer(a, b if i % 2 == 0 else c, "10000.00")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(one, range(20)))

    assert_no_unexpected(results)
    assert results.count("ok") == 10
    assert results.count("insufficient_funds") == 10
    assert balance_of(db, a) == Decimal("0.00")
    assert balance_of(db, b) + balance_of(db, c) == Decimal("300000.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_last_poisha_is_awarded_exactly_once(db, alice, bob, mallory):
    """Alice has 100,000. Fifty threads race for a 100,000 transfer.

    Exactly one can win. This is the narrowest version of the double-spend
    race: no partial successes are possible, so any answer other than one is
    unambiguous evidence of a broken lock.
    """
    a, b, c = alice.id, bob.id, mallory.id

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(
            pool.map(lambda i: transfer(a, b if i % 2 else c, "100000.00"), range(50))
        )

    assert_no_unexpected(results)
    assert results.count("ok") == 1, f"{results.count('ok')} threads won a single balance"
    assert balance_of(db, a) == Decimal("0.00")
    assert ledger_sum(db) == Decimal("0.00")


# --- lifecycle racing money ----------------------------------------------


def test_approve_and_decline_racing_the_same_request(db, alice, bob):
    """One of them wins; the request cannot end up both paid and declined."""
    req = make_request(db, bob, alice, "1000.00")
    alice_id, req_id = alice.id, req.id
    gate = threading.Barrier(2)

    def do_approve():
        gate.wait()
        return approve(alice_id, req_id)

    def do_decline():
        gate.wait()
        return decline(alice_id, req_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(do_approve), pool.submit(do_decline)]
        results = [f.result(timeout=60) for f in futures]

    assert_no_unexpected(results)
    assert results.count("ok") == 1, f"both actions succeeded: {results}"

    status = db.execute(
        text("SELECT status FROM money_requests WHERE id = :i"), {"i": str(req_id)}
    ).scalar_one()
    settlements = db.execute(
        text("SELECT COUNT(*) FROM transactions WHERE type = 'REQUEST_SETTLEMENT'")
    ).scalar_one()

    if status == "APPROVED":
        assert settlements == 1
        assert balance_of(db, alice_id) == Decimal("99000.00")
    else:
        assert status == "DECLINED"
        assert settlements == 0
        assert balance_of(db, alice_id) == Decimal("100000.00")
    assert ledger_sum(db) == Decimal("0.00")


def test_cancel_racing_approve(db, alice, bob):
    """The requester withdraws at the moment the payer pays."""
    req = make_request(db, bob, alice, "1000.00")
    alice_id, bob_id, req_id = alice.id, bob.id, req.id
    gate = threading.Barrier(2)

    def do_approve():
        gate.wait()
        return approve(alice_id, req_id)

    def do_cancel():
        gate.wait()
        db = SessionLocal()
        try:
            request_service.cancel(db, requester=db.get(User, bob_id), request_id=req_id)
            return "ok"
        except DomainError as exc:
            return exc.code
        except Exception as exc:
            return f"UNEXPECTED:{type(exc).__name__}:{str(exc)[:120]}"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(do_approve), pool.submit(do_cancel)]
        results = [f.result(timeout=60) for f in futures]

    assert_no_unexpected(results)
    assert results.count("ok") == 1, f"the request was both paid and withdrawn: {results}"
    assert ledger_sum(db) == Decimal("0.00")


def test_logout_racing_a_transfer_never_half_completes(client, db, alice, bob, auth_headers):
    """Revoking the session mid-flight either stops the transfer or does not.

    What must never happen is money moving on a session the server has already
    decided is dead, or a transfer that debits without crediting.
    """
    h = auth_headers(alice.phone)
    results = []

    def spend():
        for i in range(12):
            r = client.post(
                "/api/transfers",
                json={"recipient_phone": bob.phone, "amount": "100.00", "pin": "8317"},
                headers={**h, "Idempotency-Key": str(uuid4())},
            )
            results.append(r.status_code)

    def kill():
        client.post("/api/auth/logout", headers=h)

    t1 = threading.Thread(target=spend)
    t2 = threading.Thread(target=kill)
    t1.start(); t2.start(); t1.join(timeout=60); t2.join(timeout=60)

    assert all(code in (201, 401) for code in results), results
    succeeded = results.count(201)
    assert balance_of(db, alice.id) == Decimal("100000.00") - Decimal("100.00") * succeeded
    assert ledger_sum(db) == Decimal("0.00")


# --- identity races -------------------------------------------------------


def test_simultaneous_registration_of_one_number_creates_one_account(client, db):
    """The unique index is the arbiter, not an application-level check."""
    payload = {
        "phone": "01788888888",
        "full_name": "Race Condition",
        "password": "a-fine-password",
        "pin": "7412",
    }

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.post("/api/auth/register", json=payload).status_code,
                              range(8)))

    assert codes.count(201) == 1, f"{codes.count(201)} accounts created: {codes}"
    assert all(c in (201, 409) for c in codes), codes

    users = db.execute(
        text("SELECT COUNT(*) FROM users WHERE phone = :p"), {"p": payload["phone"]}
    ).scalar_one()
    wallets = db.execute(
        text("SELECT COUNT(*) FROM wallets w JOIN users u ON u.id = w.user_id WHERE u.phone = :p"),
        {"p": payload["phone"]},
    ).scalar_one()
    assert users == 1 and wallets == 1
    assert ledger_sum(db) == Decimal("0.00")


def test_simultaneous_refresh_of_one_token_yields_one_live_session(client, db, alice):
    """Two tabs refreshing at the same moment.

    Rotation must not hand out two live descendants of one token. Whether the
    loser is rejected outright or trips reuse detection, the invariant is that
    at most one usable refresh token exists afterwards.
    """
    from tests.conftest import PASSWORD

    tokens = client.post(
        "/api/auth/login", json={"phone": alice.phone, "password": PASSWORD}
    ).json()
    original = tokens["refresh_token"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: client.post("/api/auth/refresh", json={"refresh_token": original}),
                range(4),
            )
        )

    ok = [r for r in responses if r.status_code == 200]
    assert len(ok) <= 1, f"{len(ok)} rotations succeeded from one token"
    assert all(r.status_code in (200, 401) for r in responses)

    live = db.execute(
        text("SELECT COUNT(*) FROM sessions WHERE user_id = :u AND is_blocked = FALSE"),
        {"u": str(alice.id)},
    ).scalar_one()
    assert live <= 1, f"{live} live sessions after a contested refresh"


# --- the whole thing at once ---------------------------------------------


def test_mixed_load_leaves_the_ledger_consistent(db, alice, bob, mallory):
    """Transfers, settlements and refusals interleaved across ten threads.

    Not aimed at one bug — aimed at the claim that no legal sequence of
    operations can leave the ledger in a state we cannot defend.
    """
    people = [alice.id, bob.id, mallory.id]
    reqs = [make_request(db, bob, alice, "250.00") for _ in range(6)]
    alice_id = alice.id

    def work(i):
        if i % 5 == 4 and i // 5 < len(reqs):
            return approve(alice_id, reqs[i // 5].id)
        src = people[i % 3]
        dst = people[(i + 1) % 3]
        amount = ["1.00", "999.99", "50000.00", "0.01"][i % 4]
        return transfer(src, dst, amount, key=str(uuid4()) if i % 3 == 0 else None)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(work, range(120)))

    assert_no_unexpected(results)
    assert results.count("ok") > 0

    assert ledger_sum(db) == Decimal("0.00")
    unbalanced = db.execute(
        text("SELECT COUNT(*) FROM (SELECT transaction_id FROM ledger_entries "
             "GROUP BY transaction_id HAVING SUM(amount) <> 0) x")
    ).scalar_one()
    drifted = db.execute(
        text("SELECT COUNT(*) FROM (SELECT w.id FROM wallets w "
             "LEFT JOIN ledger_entries e ON e.wallet_id = w.id "
             "GROUP BY w.id, w.balance "
             "HAVING w.balance <> COALESCE(SUM(e.amount),0)) x")
    ).scalar_one()
    negative = db.execute(
        text("SELECT COUNT(*) FROM wallets WHERE type = 'USER' AND balance < 0")
    ).scalar_one()
    circulating = db.execute(
        text("SELECT COALESCE(SUM(balance),0) FROM wallets WHERE type = 'USER'")
    ).scalar_one()

    assert (unbalanced, drifted, negative) == (0, 0, 0)
    assert circulating == Decimal("300000.00"), "money was created or destroyed"
