"""Money request contracts.

Note what a request cannot express: there is no way to say "take this money".
A request records that someone asked. Only the payer can turn it into a
transfer, and only by re-entering their PIN.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.transfer import MoneyAmount, PartyOut, PhoneStr, PinStr


class MoneyRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_phone: PhoneStr = Field(description="Who you are asking to pay you")
    amount: MoneyAmount
    note: Annotated[str | None, Field(max_length=255)] = None

    @field_validator("note")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return " ".join(v.split()) or None


class RespondToRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: PinStr = Field(description="Required to approve; approval moves your money")


class MoneyRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    direction: Literal["INCOMING", "OUTGOING"] = Field(
        description="INCOMING means you are being asked to pay"
    )
    counterparty: PartyOut
    amount: Decimal
    note: str | None
    status: Literal["PENDING", "APPROVED", "DECLINED", "CANCELLED", "EXPIRED"]
    transaction_reference: str | None = Field(
        default=None, description="Set once the request has been paid"
    )
    expires_at: datetime
    created_at: datetime
    responded_at: datetime | None = None


class MoneyRequestList(BaseModel):
    requests: list[MoneyRequestOut]
    pending_incoming: int = Field(
        description="How many requests are waiting for you to act on"
    )
