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

Expected: **165 passed** in about 4 minutes.

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
| `test_refund.py` | 15 | Reversal writes new entries and edits nothing, both sides see the correction, sender cannot claw money back, strangers get 404, grant and reversal are not refundable, double refund refused, a failed refund leaves the original refundable, eight simultaneous refunds return the money once |
| `test_race_conditions.py` | 11 | Three-way transfer cycle, transfer racing request approval, mutual approvals, draining from two directions, fifty threads racing one exact balance, approve vs. decline, cancel vs. approve, logout mid-transfer, simultaneous registration of one number, contested refresh rotation, 120 mixed operations |
| `test_edge_cases.py` | 85 | Hostile amounts, control characters, oversized and malformed inputs, token shapes, cursor tampering, pagination bounds, bcrypt's 72-byte boundary, multibyte text |
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

---

## Bugs found by the adversarial pass

After the feature suite was green we wrote `test_edge_cases.py` and
`test_race_conditions.py` specifically to break the system. They found six
defects. All six are fixed; each fix has a comment at the site explaining what
it prevents.

### 1. A refresh token could be spent four times at once

**`test_simultaneous_refresh_of_one_token_yields_one_live_session`**

`rotate_tokens` read the session with `db.get()`, checked `is_blocked`, then
wrote. Four concurrent refreshes carrying one token all read `FALSE`, all
passed, and all minted a new session — four live descendants of a token meant
to be spent once. Two browser tabs refreshing together is enough.

This is the *same class of bug* as 4.1 above, in a different table: read then
write with no lock. Fixed with `.with_for_update()` and `populate_existing`.
The loser now wakes to find the session blocked and trips reuse detection.

### 2. A null byte in a note returned 500

PostgreSQL text cannot hold U+0000 and psycopg2 raises a bare `ValueError`. A
crafted note produced an unhandled 500 on a money endpoint. Control characters
are now rejected at the schema.

### 3. An oversized `Idempotency-Key` returned 500

The column is `VARCHAR(64)`. A longer header reached PostgreSQL raw and raised
`StringDataRightTruncation`. Now bounded at the header, so FastAPI answers 422
before any work is done.

### 4. `"1e3"` was accepted as an amount — and became ৳1,000

`Decimal()` accepts far more than money does: `1e3`, `1E-2`, `NaN`,
`Infinity`. A request carrying `"1e5"` would silently transfer 100,000. Amounts
are now matched against a plain-decimal pattern *before* reaching `Decimal`.

### 5. `"٣٠٠"` was accepted as an amount — and became ৳300

Both Python's `re` and pydantic-core's Rust regex treat `\d` as Unicode-aware,
so Arabic-Indic digits matched every numeric pattern in the project — amounts,
phone numbers and PINs alike. Every one now uses `[0-9]`.

### 6. Twenty spaces was a valid password

`min_length=8` counts whitespace. Registration now requires eight
non-whitespace characters.

### And one bug in the tests themselves

Three race tests submitted one future and awaited it before submitting the
second, so a two-party `threading.Barrier` never released and the test hung
rather than failing. Worth recording because a hanging test is easy to mistake
for a deadlock in the system — it was not; PostgreSQL would have raised
`DeadlockDetected` rather than hung.
