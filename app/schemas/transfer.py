"""Transfer and statement contracts.

`extra="forbid"` on the request models is deliberate. A client that tries to
smuggle `from_wallet_id` into a transfer gets a 422 rather than having the
field silently ignored — the safe behaviour either way, but the loud version
makes the guarantee visible in a demo.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

PhoneStr = Annotated[str, Field(pattern=r"^01[3-9][0-9]{8}$", examples=["01712345678"])]
PinStr = Annotated[str, Field(pattern=r"^[0-9]{4}$", examples=["8317"])]

# --- money ---------------------------------------------------------------
#
# Decimal() is far more permissive than money is. It happily accepts "1e3",
# "1E-2", "NaN", "Infinity" and even Arabic-Indic digits like "٣٠٠" — so a
# request carrying "1e5" would silently become a transfer of 100,000 rather
# than an error. In a payments API that is not a curiosity, it is a hazard: the
# safest amount field is one that accepts exactly the notation a human would
# type and nothing else.
#
# So the string form is checked BEFORE it reaches Decimal.
# NOTE: [0-9] rather than \d, everywhere a number is validated in this
# project. Both Python's `re` and pydantic-core's Rust regex treat \d as
# Unicode-aware, so "\u0663\u0660\u0660" (Arabic-Indic three-zero-zero) matches
# \d{3} and Decimal() then happily reads it as 300. An amount field that
# accepts digits the sender cannot type is not a validated field.
_PLAIN_AMOUNT = re.compile(r"^[0-9]{1,13}(\.[0-9]{1,2})?$")


def _plain_decimal(value: object) -> object:
    if isinstance(value, Decimal):
        value = format(value, "f")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Amount must be a plain number, for example 2500.00")
    text = str(value).strip()
    if not _PLAIN_AMOUNT.match(text):
        raise ValueError(
            "Amount must be a plain number with at most two decimal places, "
            "for example 2500.00"
        )
    return Decimal(text)


# Positive, at most two decimal places, and inside NUMERIC(15,2).
MoneyAmount = Annotated[
    Decimal,
    BeforeValidator(_plain_decimal),
    Field(gt=0, max_digits=15, decimal_places=2, examples=["2500.00"]),
]


# --- free text -----------------------------------------------------------
#
# PostgreSQL text columns cannot hold U+0000, and psycopg2 raises a bare
# ValueError when asked to store one — which would surface as a 500 on a money
# endpoint. Other control characters are equally never legitimate here and
# corrupt logs and statements. Reject at the schema, so nothing reaches the
# driver.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_free_text(value: str | None) -> str | None:
    if value is None:
        return None
    if _CONTROL_CHARS.search(value):
        raise ValueError("Text cannot contain control characters.")
    return " ".join(value.split()) or None


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_phone: PhoneStr
    amount: MoneyAmount
    pin: PinStr
    note: Annotated[str | None, Field(max_length=255)] = None

    @field_validator("note")
    @classmethod
    def _clean_note(cls, v: str | None) -> str | None:
        return clean_free_text(v)


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: PinStr = Field(description="Refunding spends your money, so it needs your PIN")
    reason: Annotated[str | None, Field(max_length=255)] = None

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: str | None) -> str | None:
        return clean_free_text(v)


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
    reverses_reference: str | None = Field(
        default=None, description="On a REVERSAL, the transaction it corrects"
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
    status: str = "COMPLETED"
    refundable: bool = Field(
        default=False,
        description="True when you received this money and can still return it",
    )


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
