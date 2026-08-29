"""Test harness.

THE IMPORTANT PART: these tests run against a real PostgreSQL database, never
SQLite. SQLite serialises writes at the file level, so `test_concurrency.py`
would pass there while proving nothing about `SELECT ... FOR UPDATE`. Anyone
can write a green race test; only a real database makes it mean something.

The swap below points the engine at TEST_DATABASE_URL before `app.database` is
imported, and refuses to run if that URL does not look like a test database —
so a mistyped .env can never drop the demo data ten minutes before judging.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

# --- redirect the engine at the test database BEFORE anything binds to it ---
os.environ.setdefault("SECRET_KEY", "pytest-only-secret-key-0123456789abcdefghij")

from app.config import settings  # noqa: E402

if "test" not in settings.test_database_url.rsplit("/", 1)[-1]:
    raise RuntimeError(
        "TEST_DATABASE_URL must point at a database with 'test' in its name. "
        f"Got: {settings.test_database_url}\n"
        "This guard exists so a mistyped .env cannot wipe your demo data."
    )

settings.database_url = settings.test_database_url

from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auth_service  # noqa: E402

PASSWORD = "test-password-123"


@pytest.fixture(scope="session", autouse=True)
def schema():
    """Rebuild the test schema once per run."""
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.test_database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(schema):
    """Truncate between tests.

    TRUNCATE rather than DELETE because the append-only trigger on
    ledger_entries blocks row-level DELETE — which is exactly the guarantee we
    want in place, so the tests work around it rather than removing it.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE audit_log, money_requests, ledger_entries, transactions, "
                "sessions, wallets, users RESTART IDENTITY CASCADE"
            )
        )
        # The mint is part of the schema, so restore it after truncation.
        conn.execute(
            text(
                "INSERT INTO wallets (id, user_id, type, currency, balance) VALUES "
                "('00000000-0000-0000-0000-000000000001', NULL, 'SYSTEM', 'BDT', 0)"
            )
        )
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# --- people -------------------------------------------------------------


def make_user(db, phone: str, name: str, pin: str):
    user, _grant = auth_service.register(
        db, phone=phone, full_name=name, password=PASSWORD, pin=pin
    )
    return user


@pytest.fixture
def alice(db):
    return make_user(db, "01711111111", "Alice Rahman", "8317")


@pytest.fixture
def bob(db):
    return make_user(db, "01722222222", "Bob Karim", "4629")


@pytest.fixture
def mallory(db):
    return make_user(db, "01799999999", "Mallory Islam", "6274")


@pytest.fixture
def auth_headers(client):
    """Log a user in and return ready-to-use Authorization headers."""

    def _login(phone: str) -> dict:
        r = client.post("/api/auth/login", json={"phone": phone, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _login


# --- invariant helpers used by several test modules ---------------------


def ledger_sum(db) -> Decimal:
    return db.execute(text("SELECT COALESCE(SUM(amount),0) FROM ledger_entries")).scalar_one()


def balance_of(db, user_id) -> Decimal:
    return db.execute(
        text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": str(user_id)}
    ).scalar_one()
