"""Money requests.

A request is an invitation, never an authorization. It moves no money by
itself: only the payer can approve it, and approving it re-enters the PIN and
calls the ordinary transfer path. The request row simply records that the
conversation happened and links to the transaction it produced.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import RequestStatus, request_status_pg


class MoneyRequest(Base):
    __tablename__ = "money_requests"
    __table_args__ = (
        CheckConstraint("amount > 0", name="request_amount_positive"),
        CheckConstraint("requester_id <> payer_id", name="no_self_request"),
        Index("idx_requests_payer", "payer_id", "status"),
        Index("idx_requests_requester", "requester_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    requester_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )  # wants the money
    payer_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )  # must approve

    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[RequestStatus] = mapped_column(
        request_status_pg, nullable=False, server_default=text("'PENDING'")
    )
    transaction_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transactions.id")
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    requester: Mapped["User"] = relationship(foreign_keys=[requester_id])  # noqa: F821
    payer: Mapped["User"] = relationship(foreign_keys=[payer_id])  # noqa: F821
    transaction: Mapped["Transaction | None"] = relationship()  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<MoneyRequest {self.amount} {self.status}>"
