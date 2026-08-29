# Test Commands

Everything here runs against a **real PostgreSQL** database (`money_test`),
never SQLite. That is the whole point: SQLite serialises writes at the file
level, so the concurrency test would pass there while proving nothing about
`SELECT ... FOR UPDATE`. `tests/conftest.py` refuses to run if
`TEST_DATABASE_URL` does not have `test` in the database name, so a mistyped
`.env` cannot wipe the demo data.

## Setup, once

```bash
psql -U postgres -c "CREATE DATABASE money_test;"
```

The schema is rebuilt automatically at the start of each run
(`alembic downgrade base` then `upgrade head`), and every test starts from a
truncated database with the system mint restored.

## Run everything

```bash
pytest
```

Expected: **54 passed** in about 90 seconds.

Rate limiting is disabled by default in tests and cleared between them — a
suite that logs in dozens of times would otherwise trip the login limit and
every failure would be a false alarm. `tests/test_rate_limit.py` turns it back
on via the `limits_on` fixture for the tests whose subject it actually is.

## The one to run in front of the judges

```bash
pytest tests/test_concurrency.py -v
```

Three tests, roughly 15 seconds, and they are the strongest evidence in the
project:

| Test | What it proves |
|---|---|
| `test_no_double_spend_under_concurrency` | Alice holds ৳100,000. Twenty threads each try to send ৳10,000. **Exactly ten succeed**, Alice lands on ৳0.00, Bob on ৳200,000, and the ledger still sums to zero. |
| `test_bidirectional_transfers_do_not_deadlock` | A→B and B→A, 25 each, fired simultaneously through a barrier. Sorted lock acquisition means no deadlock; both sides finish and balances return to where they started. |
| `test_concurrent_retries_of_one_key_move_money_once` | Ten threads, one idempotency key — the double-tapped Send button. Nine lose the race on the partial unique index; all ten receive the winner's transaction; **one** row is written. |

> This test suite earned its keep. `test_no_double_spend_under_concurrency`
> caught a real lost-update bug in our own locking code — see
> [Bug found by the suite](#bug-found-by-the-suite) below.

## By category

```bash
pytest -m concurrency     # real threads, real row locks
pytest -m security        # attacks that must not work
pytest -m invariant       # the four ledger invariants
```

## Single test, with output

```bash
pytest tests/test_transfer.py::test_same_key_replays_instead_of_resending -v -s
```

## What each file covers

| File | Tests | Covers |
|---|---|---|
| `test_concurrency.py` | 3 | Lost update, deadlock, concurrent idempotent retries |
| `test_transfer.py` | 12 | Two signed entries, statement, keyset pagination, idempotency (replay, conflict, per-user scoping), overdraft, PIN, self-transfer, unknown recipient, amount validation, decimal precision |
| `test_security.py` | 11 | IDOR, cross-account reads, missing tokens, `alg:none` forgery, wrong signing key, login enumeration, lookup leakage, SQL injection, credential storage, ledger immutability |
| `test_requests.py` | 14 | Request → approve flow, settlement typed as `REQUEST_SETTLEMENT`, requester cannot self-approve, payer PIN required, strangers get 404, each side gets only its own verb, double payment, 8 simultaneous approvals, expiry, failed settlement leaves the request pending |
| `test_rate_limit.py` | 10 | Login throttling, 429 envelope, per-account rather than per-IP keying, lookup enumeration bounds, lockout outliving the rate-limit window, reads unthrottled, reconcile green, reconcile catching a planted inconsistency, reconcile requiring auth, audit trail scoped to the caller |
| `test_invariants.py` | 4 | Fresh database, signup grant provenance, 500 randomised operations, service helpers vs. raw SQL |

## Runtime invariant check

Outside pytest, the same four invariants can be asserted against the live
development database at any time:

```bash
python seed.py --check
```

Prints seven `[ok]` lines and `ALL INVARIANTS HOLD`, or names the violation.

## Bug found by the suite

Worth telling the judges, because it is the best argument for writing the test
at all.

`ledger_service.lock_wallets` issued `SELECT ... FOR UPDATE` in sorted id order
— correct SQL, correct lock, correct ordering. PostgreSQL took the row lock
exactly as intended. But `user.wallet` is eagerly joined, so the `Wallet`
object was already in the SQLAlchemy Session's identity map, and SQLAlchemy
returned the **cached Python object** rather than the freshly selected row.

The lock was real; the balance we then read was stale. That is precisely the
lost-update bug the locking exists to prevent, hiding one layer above the SQL.

Without the fix, `test_no_double_spend_under_concurrency` reports:

```
AssertionError: expected exactly 10 successes, got 20
```

Twenty transfers of ৳10,000 succeeded against a wallet holding ৳100,000, and
the `CHECK (balance >= 0)` constraint never fired — because each transaction
computed `100000 − 10000` from its own stale copy and wrote `90000`. Last
writer wins.

The fix is one execution option:

```python
.execution_options(populate_existing=True)
```

Reviewing the SQL would never have caught this. Running twenty real threads
against a real database did.
