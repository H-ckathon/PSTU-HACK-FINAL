"""Audit log.

Every authentication event and every money movement, with actor, IP and user
agent. Append-only for the same reason the ledger is: a forensic trail you can
edit is not a forensic trail.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditAction:
    """Not an enum on purpose — new actions must not require a migration."""

    REGISTERED = "REGISTERED"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    LOGOUT = "LOGOUT"
    TOKEN_REFRESHED = "TOKEN_REFRESHED"
    TOKEN_REUSE_DETECTED = "TOKEN_REUSE_DETECTED"
    PIN_FAILED = "PIN_FAILED"
    TRANSFER_COMPLETED = "TRANSFER_COMPLETED"
    TRANSFER_REJECTED = "TRANSFER_REJECTED"
    REFUND_ISSUED = "REFUND_ISSUED"
    REQUEST_CREATED = "REQUEST_CREATED"
    REQUEST_APPROVED = "REQUEST_APPROVED"
    REQUEST_DECLINED = "REQUEST_DECLINED"
    REQUEST_CANCELLED = "REQUEST_CANCELLED"
    RATE_LIMITED = "RATE_LIMITED"


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_actor", "actor_user_id", "created_at"),
        Index("idx_audit_action", "action", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id")
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<AuditLog {self.action}>"
