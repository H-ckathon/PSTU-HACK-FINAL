"""Rate limiting.

Two design decisions worth defending:

**Keyed by user when we know who they are, by IP otherwise.** An IP-only limit
is wrong in Bangladesh, where a whole office or campus can sit behind one NAT
address — one busy user would throttle everyone. An authenticated request
carries a subject, so money endpoints are limited per account and only the
unauthenticated ones (login, register) fall back to IP, which is exactly where
IP is the right key anyway.

**In-process storage, deliberately.** The demo runs one uvicorn process, so an
in-memory bucket is correct and adds no dependency. The moment there are
multiple workers each would keep its own counter, which is why `storage_uri`
takes a Redis URL — one line, no code change. Naming the limitation is better
than pretending it is not there.

Rate limits are the outer ring. Login lockout (5 failures → 15 minutes) sits
behind them and is per-account, so an attacker rotating IPs still cannot brute
force a single account.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.errors import DomainError
from app.core.security import decode_access_token

# --- limits, in one place so they are reviewable at a glance -------------
LOGIN_LIMIT = "5/minute"        # plus per-account lockout after 5 failures
REGISTER_LIMIT = "5/minute"
REFRESH_LIMIT = "10/minute"
TRANSFER_LIMIT = "10/minute"    # per ACCOUNT, not per IP
REQUEST_LIMIT = "10/minute"
LOOKUP_LIMIT = "20/minute"      # bounds phone-number enumeration


def user_or_ip(request: Request) -> str:
    """Per-account when authenticated, per-IP when not."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        try:
            payload = decode_access_token(auth[7:])
            return f"user:{payload['sub']}"
        except (DomainError, KeyError, ValueError):
            pass  # unreadable token — fall through to the IP bucket
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=user_or_ip,
    headers_enabled=True,   # X-RateLimit-* headers, visible in the demo
    # storage_uri="redis://localhost:6379",  # <- the only change needed for
    #                                           multiple workers
)


def reset_limits() -> None:
    """Clear every bucket. Used by tests, and safe to call at any time."""
    try:
        limiter.reset()
    except (AttributeError, NotImplementedError):  # pragma: no cover
        storage = getattr(limiter, "_storage", None)
        inner = getattr(storage, "storage", None)
        if hasattr(inner, "clear"):
            inner.clear()
