"""Domain errors.

Services raise these; the HTTP layer translates them. Services therefore stay
free of framework imports, which is what makes them extractable into their own
service later.

Every error carries a stable machine-readable `code` so the frontend can react
without string-matching English, and a message written for the person reading
it: what went wrong and what to do about it.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class. Never raised directly."""

    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


# --- registration / identity --------------------------------------------


class PhoneAlreadyRegistered(DomainError):
    status_code = 409
    code = "phone_already_registered"

    def __init__(self, phone: str) -> None:
        super().__init__(f"{phone} already has an account. Log in instead.")


class InvalidCredentials(DomainError):
    status_code = 401
    code = "invalid_credentials"

    def __init__(self) -> None:
        # Deliberately identical for a wrong phone and a wrong password, so the
        # response cannot be used to enumerate registered numbers.
        super().__init__("Phone number or password is incorrect.")


class AccountLocked(DomainError):
    status_code = 423
    code = "account_locked"

    def __init__(self, minutes: int) -> None:
        super().__init__(
            f"Too many failed attempts. Try again in {minutes} minute(s).",
            retry_after_minutes=minutes,
        )


class AccountInactive(DomainError):
    status_code = 403
    code = "account_inactive"

    def __init__(self) -> None:
        super().__init__("This account is deactivated.")


# --- tokens ---------------------------------------------------------------


class InvalidToken(DomainError):
    status_code = 401
    code = "invalid_token"

    def __init__(self, message: str = "Your session is no longer valid. Log in again.") -> None:
        super().__init__(message)


class TokenReuseDetected(DomainError):
    status_code = 401
    code = "token_reuse_detected"

    def __init__(self) -> None:
        super().__init__(
            "This session was ended for safety because an old token was replayed. Log in again."
        )


# --- money ----------------------------------------------------------------


class InvalidPin(DomainError):
    status_code = 403
    code = "invalid_pin"

    def __init__(self) -> None:
        super().__init__("Incorrect transaction PIN.")


class RecipientNotFound(DomainError):
    status_code = 404
    code = "recipient_not_found"

    def __init__(self, phone: str) -> None:
        super().__init__(f"No active account found for {phone}.")


class SelfTransferNotAllowed(DomainError):
    status_code = 422
    code = "self_transfer_not_allowed"

    def __init__(self) -> None:
        super().__init__("You cannot send money to yourself.")


class InsufficientFunds(DomainError):
    status_code = 422
    code = "insufficient_funds"

    def __init__(self, available: object = None) -> None:
        super().__init__(
            "Not enough balance for this transfer.",
            **({"available": str(available)} if available is not None else {}),
        )


class IdempotencyKeyConflict(DomainError):
    """Same key, different request.

    Returning the original transaction here would be actively dangerous: the
    caller asked for something else. Replay is only safe when the request is
    identical, so a mismatch is an error rather than a silent no-op.
    """

    status_code = 409
    code = "idempotency_key_conflict"

    def __init__(self, reference: str) -> None:
        super().__init__(
            "This idempotency key was already used for a different transfer "
            f"({reference}). Use a new key.",
            original_reference=reference,
        )


class TransactionNotFound(DomainError):
    status_code = 404
    code = "transaction_not_found"

    def __init__(self, reference: str) -> None:
        # Identical response whether the reference does not exist or belongs to
        # someone else, so references cannot be probed.
        super().__init__(f"No transaction {reference} on this account.")


# --- money requests -------------------------------------------------------


class RequestNotFound(DomainError):
    status_code = 404
    code = "request_not_found"

    def __init__(self) -> None:
        # Identical whether the id is unknown or belongs to someone else's
        # conversation, so request ids cannot be probed.
        super().__init__("No such money request on this account.")


class RequestNotPending(DomainError):
    status_code = 409
    code = "request_not_pending"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"This request was already {status.lower()}.", current_status=status
        )


class RequestExpired(DomainError):
    status_code = 410
    code = "request_expired"

    def __init__(self) -> None:
        super().__init__("This request has expired. Ask for a new one.")


class SelfRequestNotAllowed(DomainError):
    status_code = 422
    code = "self_request_not_allowed"

    def __init__(self) -> None:
        super().__init__("You cannot request money from yourself.")


class WalletMissing(DomainError):
    status_code = 500
    code = "wallet_missing"

    def __init__(self) -> None:
        super().__init__("Account wallet is missing. Contact support.")
