"""FastAPI application.

Blocks complete: 1 (foundation), 2 (auth), 3 (transfers), 4 (tests),
5 (money requests), 6 (rate limiting, reconciliation). Frontend is Block 7.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.constants import SYSTEM_MINT_WALLET_ID
from slowapi.errors import RateLimitExceeded

from app.core.errors import DomainError
from app.core.limiter import limiter
from app.database import SessionLocal, engine
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from app.routers import requests as requests_router
from app.routers import transfers as transfers_router
from app.routers import wallet as wallet_router

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly at boot rather than insecurely at runtime.
    settings.assert_bootable()

    with SessionLocal() as db:
        mint = db.execute(
            text("SELECT balance FROM wallets WHERE id = :id"),
            {"id": str(SYSTEM_MINT_WALLET_ID)},
        ).scalar_one_or_none()
        if mint is None:
            raise RuntimeError("SYSTEM_MINT wallet is missing. Run: alembic upgrade head")
    yield
    engine.dispose()


app = FastAPI(
    title="Money Movement API",
    version="0.6.0",
    description=(
        "A closed money ecosystem with simulated funds.\n\n"
        "Money moves only through an append-only double-entry ledger. "
        "Every transfer is idempotent, every balance is reconcilable, and "
        "no user wallet can go negative.\n\n"
        "**Try it:** register two users, then send money between them. The "
        "opening balance is a real `SIGNUP_GRANT` transaction debited from the "
        "system mint, not a number written into a column.\n\n"
        "**Then prove it:** `GET /api/admin/reconcile` asserts all four ledger "
        "invariants against live data, at any moment you like."
    ),
    lifespan=lifespan,
)


app.state.limiter = limiter


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """One place where business failures become HTTP.

    Services raise domain errors and stay framework-free; the API returns a
    stable machine-readable code plus a message written for a human.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """429 in the same envelope as every other error, with a Retry-After."""
    return JSONResponse(
        status_code=429,
        content={
            "code": "rate_limited",
            "message": "Too many requests. Wait a moment and try again.",
            "details": {"limit": str(exc.detail)},
        },
        headers={"Retry-After": "60"},
    )


app.include_router(auth_router.router)
app.include_router(wallet_router.router)
app.include_router(transfers_router.router)
app.include_router(requests_router.router)
app.include_router(admin_router.router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness — the process is up."""
    return {"status": "ok", "version": app.version}


@app.get("/ready", tags=["ops"])
def ready() -> dict:
    """Readiness — the database answers and the ledger still balances."""
    with SessionLocal() as db:
        ledger_total = db.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        ).scalar_one()
        entry_count = db.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar_one()
    return {
        "status": "ok",
        "database": "connected",
        "ledger_entries": entry_count,
        "ledger_sum": str(ledger_total),
        "balanced": ledger_total == 0,
    }


# The frontend is served by this same process: one origin, no CORS, no build.
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
