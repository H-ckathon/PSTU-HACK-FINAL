"""Pydantic v2 request/response contracts.

These are the security boundary. Bad input dies here, never inside money logic.
Note what is absent: no schema anywhere accepts a wallet id from the client.
The sender is always derived from the access token, which closes the
broken-object-level-authorization (IDOR) class by construction.
"""
