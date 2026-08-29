"""Users and sessions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import INET, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Phone, not email: it is how a Bangladeshi user identifies another user.
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Two independent secrets. The password opens the session; the PIN
    # authorises an individual money movement. A stolen session is not enough.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))
    failed_login_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    wallet: Mapped["Wallet"] = relationship(  # noqa: F821
        back_populates="user", uselist=False, lazy="joined"
    )
    sessions: Mapped[list["Session"]] = relationship(back_populates="user")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User {self.phone} {self.full_name!r}>"


class Session(Base):
    """Refresh-token family.

    The access token is stateless and short-lived; this row is what makes
    logout and revocation real. Rotation on refresh, and reuse of a rotated
    token blocks the whole family.
    """

    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)  # refresh jti
    # Every rotation inherits the family of the token it replaced, so one reuse
    # detection can revoke the entire chain rather than a single link.
    family_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    refresh_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    client_ip: Mapped[str | None] = mapped_column(INET)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="sessions")
