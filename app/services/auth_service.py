"""Registration, login, token rotation, logout.

Registration is the first place the ledger discipline shows up: the signup
grant is a real SIGNUP_GRANT transaction debiting SYSTEM_MINT and crediting the
new wallet — never `INSERT INTO wallets (balance) VALUES (100000)`. Every taka
in the closed ecosystem therefore has a provable origin, and the global ledger
sums to zero from the very first user onward.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.constants import SYSTEM_MINT_WALLET_ID
from app.core import security
from app.core.errors import (
    AccountInactive,
    AccountLocked,
    InvalidCredentials,
    InvalidToken,
    PhoneAlreadyRegistered,
    TokenReuseDetected,
)
from app.models import (
    AuditAction,
    AuditLog,
    Session,
    Transaction,
    TxnType,
    User,
    Wallet,
    WalletType,
)
from app.services import ledger_service

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


# --- audit helper ---------------------------------------------------------


def audit(
    db: DbSession,
    action: str,
    *,
    actor: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    **meta,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=ip,
            user_agent=(user_agent or "")[:255] or None,
            meta=meta or None,
        )
    )


# --- registration ---------------------------------------------------------


def register(
    db: DbSession,
    *,
    phone: str,
    full_name: str,
    password: str,
    pin: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, Transaction]:
    """Create user + wallet + funded signup grant, atomically.

    Returns (user, grant_transaction). Commits on success.
    """
    if db.execute(select(User.id).where(User.phone == phone)).scalar_one_or_none():
        raise PhoneAlreadyRegistered(phone)

    try:
        user = User(
            phone=phone,
            full_name=full_name,
            password_hash=security.hash_secret(password),
            pin_hash=security.hash_secret(pin),
        )
        db.add(user)
        db.flush()

        wallet = Wallet(user_id=user.id, type=WalletType.USER, currency=settings.currency)
        db.add(wallet)
        db.flush()

        # Lock the mint. This serialises concurrent registrations, which is
        # correct and irrelevant at any plausible signup rate; at real scale
        # grants would be drawn from pre-funded pool wallets so the mint stops
        # being a single hot row.
        locked = ledger_service.lock_wallets(db, SYSTEM_MINT_WALLET_ID, wallet.id)
        mint = locked[SYSTEM_MINT_WALLET_ID]
        wallet = locked[wallet.id]

        grant = ledger_service.post_double_entry(
            db,
            txn_type=TxnType.SIGNUP_GRANT,
            amount=settings.signup_grant,
            debit_wallet=mint,          # the mint goes negative by design
            credit_wallet=wallet,
            initiated_by=user.id,
            note="Welcome bonus",
            allow_overdraft=True,       # SYSTEM wallets only
        )

        audit(
            db,
            AuditAction.REGISTERED,
            actor=user.id,
            entity_type="user",
            entity_id=user.id,
            ip=ip,
            user_agent=user_agent,
            grant=str(settings.signup_grant),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "users_phone_key" in str(exc.orig):
            raise PhoneAlreadyRegistered(phone) from exc
        raise

    db.refresh(user)
    return user, grant


# --- login ----------------------------------------------------------------


def authenticate(
    db: DbSession,
    *,
    phone: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> User:
    user = db.execute(select(User).where(User.phone == phone)).scalar_one_or_none()

    if user is None:
        # Spend comparable time so response latency cannot be used to tell a
        # registered number from an unregistered one.
        security.burn_dummy_verification()
        audit(db, AuditAction.LOGIN_FAILED, ip=ip, user_agent=user_agent, phone=phone)
        db.commit()
        raise InvalidCredentials()

    now = datetime.now(UTC)
    if user.locked_until and user.locked_until > now:
        remaining = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        raise AccountLocked(remaining)

    if not user.is_active:
        raise AccountInactive()

    if not security.verify_secret(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0
            audit(
                db,
                AuditAction.ACCOUNT_LOCKED,
                actor=user.id,
                ip=ip,
                user_agent=user_agent,
                minutes=LOCKOUT_MINUTES,
            )
            db.commit()
            raise AccountLocked(LOCKOUT_MINUTES)
        audit(
            db,
            AuditAction.LOGIN_FAILED,
            actor=user.id,
            ip=ip,
            user_agent=user_agent,
            attempt=user.failed_login_count,
        )
        db.commit()
        raise InvalidCredentials()

    user.failed_login_count = 0
    user.locked_until = None
    audit(db, AuditAction.LOGIN_SUCCESS, actor=user.id, ip=ip, user_agent=user_agent)
    db.commit()
    return user


# --- tokens ---------------------------------------------------------------


def issue_tokens(
    db: DbSession,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, int]:
    """Returns (access_token, refresh_token, expires_in)."""
    raw_refresh, session_id, stored_hash = security.new_refresh_token()

    db.add(
        Session(
            id=session_id,
            family_id=session_id,   # a fresh login starts a new family
            user_id=user.id,
            refresh_hash=stored_hash,
            user_agent=(user_agent or "")[:255] or None,
            client_ip=ip,
            expires_at=security.refresh_expiry(),
        )
    )
    db.commit()

    access, expires_in = security.create_access_token(user.id, session_id)
    return access, raw_refresh, expires_in


def _revoke_family(db: DbSession, family_id: UUID) -> None:
    """Kill every session descended from one login. One statement, no loop."""
    db.execute(
        update(Session).where(Session.family_id == family_id).values(is_blocked=True)
    )


def rotate_tokens(
    db: DbSession,
    raw_refresh: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, int]:
    """Rotate a refresh token.

    Replaying an already-rotated token is the signature of a stolen token, so
    it blocks the session rather than merely failing.
    """
    session_id = security.split_refresh_token(raw_refresh)

    # Lock the session row for the whole rotation.
    #
    # Without this, rotation is a read-then-write race: several requests
    # carrying the same refresh token all read `is_blocked = FALSE`, all pass
    # the check, and all mint a new session — leaving four live descendants of
    # a token that was supposed to be spent once. Two browser tabs refreshing
    # together is enough to trigger it. `populate_existing` matters for the
    # same reason it does in the ledger: without it SQLAlchemy would hand back
    # the identity-map copy and we would re-read stale state under a real lock.
    #
    # Serialising here means the loser wakes to find `is_blocked = TRUE` and
    # trips reuse detection. That does mean a genuine double-tab refresh ends
    # the session — the accepted trade in token rotation, since nothing at the
    # server can distinguish an honest race from a stolen token replayed a
    # moment later. Production systems soften it with a short grace window;
    # we chose the strict, safe default.
    session = db.execute(
        select(Session).where(Session.id == session_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    if session is None:
        raise InvalidToken()

    if session.is_blocked:
        # A blocked session being presented is either a replay of a spent
        # token or a stolen one. Either way the whole family is compromised.
        _revoke_family(db, session.family_id)
        audit(
            db,
            AuditAction.TOKEN_REUSE_DETECTED,
            actor=session.user_id,
            ip=ip,
            family=str(session.family_id),
            reason="blocked_session_replayed",
        )
        db.commit()
        raise TokenReuseDetected()

    if session.expires_at <= datetime.now(UTC):
        raise InvalidToken("Your session expired. Log in again.")

    if security.hash_refresh_token(raw_refresh) != session.refresh_hash:
        # Right session id, wrong secret: forged, or tampered with.
        _revoke_family(db, session.family_id)
        audit(
            db,
            AuditAction.TOKEN_REUSE_DETECTED,
            actor=session.user_id,
            ip=ip,
            family=str(session.family_id),
            reason="secret_mismatch",
        )
        db.commit()
        raise TokenReuseDetected()

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise InvalidToken()

    new_raw, new_id, new_hash = security.new_refresh_token()
    session.is_blocked = True  # retire the link, keep the family alive
    db.add(
        Session(
            id=new_id,
            family_id=session.family_id,   # the chain continues
            user_id=user.id,
            refresh_hash=new_hash,
            user_agent=(user_agent or "")[:255] or None,
            client_ip=ip,
            expires_at=security.refresh_expiry(),
        )
    )
    audit(db, AuditAction.TOKEN_REFRESHED, actor=user.id, ip=ip, user_agent=user_agent)
    db.commit()

    access, expires_in = security.create_access_token(user.id, new_id)
    return access, new_raw, expires_in


def logout(db: DbSession, session_id: UUID, *, ip: str | None = None) -> None:
    session = db.get(Session, session_id)
    if session is not None:
        session.is_blocked = True
        audit(db, AuditAction.LOGOUT, actor=session.user_id, ip=ip)
        db.commit()
