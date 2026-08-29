"""Wallets.

`balance` is a PROJECTION, not the source of truth. Truth is the signed sum of
ledger_entries for this wallet. The projection is updated inside the same
transaction that appends the entries, and `/api/admin/reconcile` proves the two
still agree.

The `no_overdraft` CHECK is the money guarantee: even with a bug in the service
layer, PostgreSQL will not store a negative USER balance.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import WalletType, wallet_type_pg


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint(
            "(type = 'USER' AND user_id IS NOT NULL) OR "
            "(type = 'SYSTEM' AND user_id IS NULL)",
            name="wallet_owner",
        ),
        CheckConstraint(
            "type = 'SYSTEM' OR balance >= 0",
            name="no_overdraft",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), unique=True
    )
    type: Mapped[WalletType] = mapped_column(
        wallet_type_pg, nullable=False, server_default=text("'USER'")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'BDT'"))
    balance: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User | None"] = relationship(back_populates="wallet")  # noqa: F821

    @property
    def is_system(self) -> bool:
        return self.type == WalletType.SYSTEM

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Wallet {self.type} {self.balance}>"
