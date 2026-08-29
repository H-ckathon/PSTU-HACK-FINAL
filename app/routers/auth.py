"""Authentication and profile endpoints.

NOTE: no `from __future__ import annotations` in the routers that carry
`@limiter.limit`. That import turns annotations into strings, and slowapi's
wrapper reports its OWN module globals, so FastAPI cannot resolve
`TransferRequest` or `LoginRequest` and silently demotes the request body to a
query parameter. Every write endpoint then 422s with
`{"loc": ["query", "body"]}`. Keep annotations real in these files.
"""

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.deps import Principal, client_ip, get_current_principal, get_current_user
from app.core.errors import RecipientNotFound
from app.core.limiter import (
    LOGIN_LIMIT,
    LOOKUP_LIMIT,
    REFRESH_LIMIT,
    REGISTER_LIMIT,
    limiter,
)
from app.database import get_db
from app.models import User
from app.schemas.auth import (
    LoginRequest,
    LookupOut,
    MeOut,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenPair,
)
from app.services import auth_service

router = APIRouter(prefix="/api", tags=["auth"])


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.post(
    "/auth/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and fund it from the system mint",
)
@limiter.limit(REGISTER_LIMIT)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
) -> RegisterResponse:
    """The signup grant is a real ledger transaction, not a balance write.

    Look at the response `grant_reference`, then at `GET /api/wallet/statement`:
    the opening balance has an origin entry, and the system mint holds the
    matching debit.
    """
    user, grant = auth_service.register(
        db,
        phone=body.phone,
        full_name=body.full_name,
        password=body.password,
        pin=body.pin,
        ip=client_ip(request),
        user_agent=_ua(request),
    )
    access, refresh, expires_in = auth_service.issue_tokens(
        db, user, ip=client_ip(request), user_agent=_ua(request)
    )
    return RegisterResponse(
        user=user,
        wallet=user.wallet,
        tokens=TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in),
        grant_reference=grant.reference,
    )


@router.post("/auth/login", response_model=TokenPair, summary="Exchange credentials for tokens")
@limiter.limit(LOGIN_LIMIT)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
) -> TokenPair:
    user = auth_service.authenticate(
        db,
        phone=body.phone,
        password=body.password,
        ip=client_ip(request),
        user_agent=_ua(request),
    )
    access, refresh, expires_in = auth_service.issue_tokens(
        db, user, ip=client_ip(request), user_agent=_ua(request)
    )
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/auth/refresh", response_model=TokenPair, summary="Rotate the refresh token")
@limiter.limit(REFRESH_LIMIT)
def refresh(
    body: RefreshRequest,
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
) -> TokenPair:
    """Rotation with reuse detection: replaying a spent token ends the session."""
    access, new_refresh, expires_in = auth_service.rotate_tokens(
        db, body.refresh_token, ip=client_ip(request), user_agent=_ua(request)
    )
    return TokenPair(access_token=access, refresh_token=new_refresh, expires_in=expires_in)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="End this session immediately",
)
def logout(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: DbSession = Depends(get_db),
) -> Response:
    """Real revocation: the access token stops working on the next request."""
    auth_service.logout(db, principal.session_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut, summary="Current profile and balance")
def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(user=user, wallet=user.wallet)


@router.get(
    "/users/lookup",
    response_model=LookupOut,
    summary="Resolve a phone number to a name before sending",
)
@limiter.limit(LOOKUP_LIMIT)
def lookup(
    request: Request,
    response: Response,
    phone: str = Query(pattern=r"^01[3-9][0-9]{8}$", examples=["01712345678"]),
    db: DbSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> LookupOut:
    """Returns a name and nothing else — never a balance.

    So the sender can confirm who they are paying before they pay, and phone
    enumeration still reveals nothing financial.
    """
    target = db.execute(
        select(User).where(User.phone == phone, User.is_active.is_(True))
    ).scalar_one_or_none()
    if target is None:
        raise RecipientNotFound(phone)
    return LookupOut(phone=target.phone, full_name=target.full_name)
