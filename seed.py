"""Database lifecycle helper.

    python seed.py --check          verify schema, mint wallet and all invariants
    python seed.py --demo           create the demo users (Alice and Bob)
    python seed.py --reset --demo   drop, re-migrate, re-create demo users, verify

`python seed.py --reset --demo` is what you run immediately before the judges'
demo so the run starts from a known-clean, known-funded state. It takes a
couple of seconds, and rehearsing it is how you recover from a bad demo.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from decimal import Decimal

from sqlalchemy import text

from app.config import settings
from app.constants import SYSTEM_MINT_WALLET_ID
from app.database import SessionLocal

OK = "  [ok]  "
BAD = "  [FAIL]"


def _alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"alembic {' '.join(args)} failed")


def reset() -> None:
    print("Resetting database ...")
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    print("Schema rebuilt.\n")


DEMO_USERS = [
    # phone,        name,            password,        pin
    ("01711111111", "Alice Rahman", "alice-demo-pass", "8317"),
    ("01722222222", "Bob Karim", "bob-demo-pass", "4629"),
    ("01733333333", "Chowdhury Nabil", "nabil-demo-pass", "5182"),
]


def demo() -> None:
    """Create the demo users, each funded by a real SIGNUP_GRANT transaction."""
    from app.core.errors import PhoneAlreadyRegistered
    from app.services import auth_service

    print("Creating demo users ...")
    with SessionLocal() as db:
        for phone, name, password, pin in DEMO_USERS:
            try:
                user, grant = auth_service.register(
                    db, phone=phone, full_name=name, password=password, pin=pin
                )
            except PhoneAlreadyRegistered:
                print(f"  ~  {phone}  {name}  (already exists)")
                continue
            print(
                f"  +  {phone}  {name:<18} balance {user.wallet.balance}"
                f"  grant {grant.reference}"
            )
    print("\n  Login with the phone number and password above; the PIN authorises transfers.\n")


def check() -> bool:
    """Assert every invariant from the architecture spec. Returns True if clean."""
    print(f"Database : {settings.database_url.rsplit('@', 1)[-1]}")
    passed = True

    with SessionLocal() as db:
        # --- schema present ------------------------------------------------
        tables = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        }
        expected = {
            "users",
            "sessions",
            "wallets",
            "transactions",
            "ledger_entries",
            "money_requests",
            "audit_log",
        }
        missing = expected - tables
        if missing:
            print(f"{BAD} missing tables: {', '.join(sorted(missing))}")
            print("       run: alembic upgrade head")
            return False
        print(f"{OK} all 7 tables present")

        # --- system mint ---------------------------------------------------
        mint = db.execute(
            text("SELECT balance FROM wallets WHERE id = :id AND type = 'SYSTEM'"),
            {"id": str(SYSTEM_MINT_WALLET_ID)},
        ).scalar_one_or_none()
        if mint is None:
            print(f"{BAD} SYSTEM_MINT wallet missing")
            return False
        print(f"{OK} SYSTEM_MINT present, balance {mint}  (negative = money issued)")

        # --- invariant 01: conservation -----------------------------------
        total = db.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        ).scalar_one()
        if total != Decimal("0.00"):
            print(f"{BAD} invariant 01 conservation: ledger sums to {total}, expected 0")
            passed = False
        else:
            print(f"{OK} invariant 01 conservation      SUM(entries) = 0")

        # --- invariant 02: balanced events --------------------------------
        unbalanced = db.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT transaction_id FROM ledger_entries"
                "  GROUP BY transaction_id HAVING SUM(amount) <> 0"
                ") x"
            )
        ).scalar_one()
        if unbalanced:
            print(f"{BAD} invariant 02 balanced events: {unbalanced} unbalanced transaction(s)")
            passed = False
        else:
            print(f"{OK} invariant 02 balanced events   SUM per txn = 0")

        # --- invariant 03: projection matches ledger -----------------------
        drifted = db.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT w.id FROM wallets w"
                "  LEFT JOIN ledger_entries e ON e.wallet_id = w.id"
                "  GROUP BY w.id, w.balance"
                "  HAVING w.balance <> COALESCE(SUM(e.amount), 0)"
                ") x"
            )
        ).scalar_one()
        if drifted:
            print(f"{BAD} invariant 03 no drift: {drifted} wallet(s) disagree with the ledger")
            passed = False
        else:
            print(f"{OK} invariant 03 no drift          balance = SUM(entries)")

        # --- invariant 04: solvency ----------------------------------------
        negative = db.execute(
            text("SELECT COUNT(*) FROM wallets WHERE type = 'USER' AND balance < 0")
        ).scalar_one()
        if negative:
            print(f"{BAD} invariant 04 solvency: {negative} negative user wallet(s)")
            passed = False
        else:
            print(f"{OK} invariant 04 solvency          all USER balances >= 0")

        # --- append-only enforcement is live -------------------------------
        triggers = db.execute(
            text(
                "SELECT COUNT(*) FROM pg_trigger "
                "WHERE tgname IN ('trg_ledger_append_only','trg_audit_append_only')"
            )
        ).scalar_one()
        if triggers != 2:
            print(f"{BAD} append-only triggers missing (found {triggers}/2)")
            passed = False
        else:
            print(f"{OK} append-only triggers armed on ledger_entries and audit_log")

        users = db.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        txns = db.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()
        entries = db.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar_one()

    print(f"\nUsers {users} · transactions {txns} · ledger entries {entries}")
    print("\nALL INVARIANTS HOLD." if passed else "\nINVARIANT VIOLATION — do not demo this state.")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(description="Money Movement database helper")
    parser.add_argument("--reset", action="store_true", help="drop, re-migrate, then verify")
    parser.add_argument("--demo", action="store_true", help="create the demo users")
    parser.add_argument("--check", action="store_true", help="verify only (default)")
    args = parser.parse_args()

    if args.reset:
        reset()
    if args.demo:
        demo()

    raise SystemExit(0 if check() else 1)


if __name__ == "__main__":
    main()
