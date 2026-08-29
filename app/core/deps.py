"""FastAPI dependencies.

`get_current_user` is where every protected endpoint gets its identity. Note
that identity comes from the token and ONLY from the token — no endpoint
accepts a user id or wallet id from the client, which is what closes the
broken-object-level-authorization class by construction rather than by a check
someone might forget to write.
"""

from __future__ import annotations

from ipaddress import ip_address
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as DbSession

from app.core.errors import AccountInactive, InvalidToken
from app.core.security import decode_access_token
from app.database import get_db
from app.models import Session, User

bearer = HTTPBearer(auto_error=False, description="Access token from /api/auth/login")


class Principal:
    """The authenticated caller: who they are, and which session they are on."""

    __slots__ = ("user", "session_id")

    def __init__(self, user: User, session_id: UUID) -> None:
        self.user = user
        self.session_id = session_id

    @property
    def id(self) -> UUID:
        return self.user.id


def client_ip(request: Request) -> str | None:
    """The caller's IP, or None if it is not a parseable address.

    `audit_log.ip_address` is a PostgreSQL INET column, which gives us free
    validation and real network-range queries later — but it rejects anything
    that is not an address. A proxy, a test client or a unix socket can all
    present a non-IP host, and an audit write must never be the thing that
    fails a money operation. So the value is validated here, at the edge, and
    the column keeps its strong type.
    """
    if not request.client:
        return None
    host = request.client.host
    try:
        ip_address(host)
    except ValueError:
        return None
    return host


def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: DbSession = Depends(get_db),
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise InvalidToken("Authentication required.")

    payload = decode_access_token(credentials.credentials)

    try:
        user_id = UUID(payload["sub"])
        session_id = UUID(payload["sid"])
    except (KeyError, ValueError) as exc:
        raise InvalidToken() from exc

    # One indexed primary-key lookup per request buys instant revocation:
    # logout takes effect immediately instead of after the token expires. At
    # scale this becomes a Redis denylist; the trade-off is deliberate and the
    # cost is a single cached row.
    session = db.get(Session, session_id)
    if session is None or session.is_blocked:
        raise InvalidToken("Your session was ended. Log in again.")

    user = db.get(User, user_id)
    if user is None:
        raise InvalidToken()
    if not user.is_active:
        raise AccountInactive()

    return Principal(user, session_id)


def get_current_user(principal: Principal = Depends(get_current_principal)) -> User:
    return principal.user
