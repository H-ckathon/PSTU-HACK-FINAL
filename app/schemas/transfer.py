"""Transfer and statement contracts.

`extra="forbid"` on the request models is deliberate. A client that tries to
smuggle `from_wallet_id` into a transfer gets a 422 rather than having the
field silently ignored — the safe behaviour either way, but the loud version
makes the guarantee visible in a demo.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

PhoneStr = Annotated[str, Field(pattern=r"^01[3-9]\d{8}$", examples=["01712345678"])]
PinStr = Annotated[str, Field(pattern=r"^\d{4}$", examples=["8317"])]

# Positive, at most two decimal places, and inside NUMERIC(15,2).
MoneyAmount = Annotated[
    Decimal,
    Field(gt=0, max_digits=15, decimal_places=2, examples=["2500.00"]),
]


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_phone: PhoneStr
    amount: MoneyAmount
    pin: PinStr
    note: Annotated[str | None, Field(max_length=255)] = None

    @field_validator("note")
    @classmethod
    def _clean_note(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = " ".join(v.split())
        return v or None


class PartyOut(BaseModel):
    phone: str
    full_name: str


class TransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reference: str
    status: str
    type: str
    amount: Decimal
    note: str | None
    created_at: datetime
    completed_at: datetime | None
    sender: PartyOut | None = None
    recipient: PartyOut | None = None
    balance_after: Decimal | None = Field(
        default=None, description="Your balance after this transaction"
    )
    idempotent_replay: bool = Field(
        default=False,
        description="True when this response replays an earlier identical request",
    )


class StatementEntry(BaseModel):
    """One line of a bank statement, from the account holder's point of view."""

    id: int
    reference: str
    type: str
    direction: Literal["DEBIT", "CREDIT"]
    amount: Decimal = Field(description="Signed: negative is money out")
    balance_after: Decimal
    counterparty: PartyOut | None
    note: str | None
    created_at: datetime


class StatementPage(BaseModel):
    entries: list[StatementEntry]
    next_cursor: str | None = Field(
        default=None, description="Pass as ?cursor= to fetch the next page"
    )
    balance: Decimal


class ReconcileReport(BaseModel):
    """The four invariants, asserted live."""

    conservation: bool = Field(description="SUM(all ledger entries) = 0")
    balanced_events: bool = Field(description="SUM(entries) = 0 for every transaction")
    no_drift: bool = Field(description="wallets.balance = SUM(its entries)")
    solvency: bool = Field(description="no USER wallet is negative")
    all_hold: bool
    ledger_sum: Decimal
    wallet_count: int
    transaction_count: int
    entry_count: int
    offending: dict[str, list[UUID]] = Field(default_factory=dict)
