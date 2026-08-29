"""Fixed identifiers that must be stable across migrations, seeds and tests."""

from uuid import UUID

# The system mint. Every taka in the closed ecosystem originates here, which is
# why this wallet is allowed to hold a negative balance and every other wallet
# is not. Created by migration 0001 so it always exists.
SYSTEM_MINT_WALLET_ID = UUID("00000000-0000-0000-0000-000000000001")

# Human-readable transaction reference alphabet: no 0/O/1/I to avoid
# transcription errors when someone reads a reference aloud.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
REFERENCE_PREFIX = "TXN"
REFERENCE_BODY_LENGTH = 8
