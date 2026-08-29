"""Auth and profile contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.transfer import clean_free_text

# Bangladeshi mobile format: 01[3-9] followed by 8 digits.
PhoneStr = Annotated[str, Field(pattern=r"^01[3-9][0-9]{8}$", examples=["01712345678"])]

# bcrypt truncates beyond 72 bytes, so we cap rather than silently shorten.
PasswordStr = Annotated[str, Field(min_length=8, max_length=72)]
PinStr = Annotated[str, Field(pattern=r"^[0-9]{4}$", examples=["1234"])]


class RegisterRequest(BaseModel):
    phone: PhoneStr
    full_name: Annotated[str, Field(min_length=2, max_length=100)]
    password: PasswordStr
    pin: PinStr

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        cleaned = clean_free_text(v)
        if not cleaned:
            raise ValueError("Full name cannot be blank.")
        return cleaned

    @field_validator("password")
    @classmethod
    def _password_has_substance(cls, v: str) -> str:
        # min_length alone accepts twenty spaces, which is a password only in
        # the sense that it is hard to remember.
        if not v.strip():
            raise ValueError("Password cannot be only whitespace.")
        if len(v.strip()) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v

    @field_validator("pin")
    @classmethod
    def _not_trivial_pin(cls, v: str) -> str:
        if v in {"0000", "1111", "2222", "3333", "4444",
                 "5555", "6666", "7777", "8888", "9999", "1234"}:
            raise ValueError("Choose a less predictable PIN.")
        return v


class LoginRequest(BaseModel):
    phone: PhoneStr
    password: PasswordStr


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")


class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    balance: Decimal
    currency: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    phone: str
    full_name: str
    created_at: datetime


class MeOut(BaseModel):
    user: UserOut
    wallet: WalletOut


class RegisterResponse(BaseModel):
    user: UserOut
    wallet: WalletOut
    tokens: TokenPair
    grant_reference: str = Field(
        description="Reference of the SIGNUP_GRANT transaction that funded this wallet"
    )


class LookupOut(BaseModel):
    """Deliberately minimal: a name, never a balance.

    Phone enumeration therefore reveals nothing financial.
    """

    phone: str
    full_name: str
