# Money Movement Application

**PSTU IT Carnival 2026 — Hackathon Challenge**

A closed money ecosystem with simulated funds. Money moves only through an
append-only double-entry ledger; every transfer is idempotent, every balance is
reconcilable against the ledger, and no user wallet can go negative.

> Build status: **Blocks 1–5 of 7 complete** — schema, auth, the transfer path,
> money requests, and a 44-test suite running against real PostgreSQL. Rate
> limiting, the reconcile endpoint and the frontend remain.

---

## 1. Run it

Requires PostgreSQL 16 running locally and Python 3.11+.

```bash
# 1. create the databases (once)
psql -U postgres -c "CREATE DATABASE money;"
psql -U postgres -c "CREATE DATABASE money_test;"

# 2. dependencies
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

# 3. configuration
copy .env.example .env          # Windows   (cp on macOS/Linux)
python -c "import secrets; print(secrets.token_urlsafe(48))"
#    paste the output into SECRET_KEY in .env, and fix the DB password

# 4. schema
alembic upgrade head

# 5. demo users + verify — this prints all four invariants
python seed.py --demo

# 6. run
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Then open **http://127.0.0.1:8000/docs**.

Before demoing, reset to a known-clean, known-funded state:

```bash
python seed.py --reset --demo
```

### Demo accounts

Each is funded with ৳100,000 by a real `SIGNUP_GRANT` transaction, not a
balance write.

| Phone | Name | Password | PIN |
|---|---|---|---|
| `01711111111` | Alice Rahman | `alice-demo-pass` | `8317` |
| `01722222222` | Bob Karim | `bob-demo-pass` | `4629` |
| `01733333333` | Chowdhury Nabil | `nabil-demo-pass` | `5182` |

The password opens a session; the PIN authorises each individual transfer.

---

## 2. Architecture in one paragraph

A **modular monolith** over PostgreSQL. One FastAPI process serves the API and
the frontend from the same origin. Business rules live in `app/services/`, which
imports no framework code, so any module can be extracted into its own service
without a rewrite. The money path is deliberately the only complex part of the
system: a single ACID transaction that locks both wallets in a deterministic
order, appends two signed ledger entries that sum to zero, and updates the
balance projection — all or nothing.

Full design document: `docs/ARCHITECTURE.md`.

---

## 3. The four invariants

These are the entire correctness argument. `python seed.py --check` asserts all
four, and so does `GET /api/admin/reconcile` at runtime.

| # | Invariant | Meaning |
|---|---|---|
| 01 | `SUM(ledger_entries.amount) = 0` | Money is never created or destroyed |
| 02 | `SUM(amount) = 0` per `transaction_id` | Every event debits exactly what it credits |
| 03 | `wallets.balance = SUM(entries)` | The projection never drifts from the ledger |
| 04 | `balance >= 0` for USER wallets | Enforced by a CHECK constraint, not by code |

---

## 4. Data model

Seven tables. Every guarantee that matters is a database constraint, so
correctness survives a bug in the application layer.

| Table | Role |
|---|---|
| `users` | Identity. Two independent secrets: a password for the session, a PIN for each money movement. |
| `sessions` | Refresh-token families. Gives short-lived stateless JWTs a real revocation path. |
| `wallets` | One per user, plus `SYSTEM_MINT`. `balance` is a projection; `CHECK (balance >= 0)` is the money guarantee. |
| `transactions` | One row per business event, with a state machine and an idempotency key. |
| `ledger_entries` | **Append-only, signed, the source of truth.** Two rows per transfer, summing to zero. |
| `money_requests` | Collect-money flow. A request is an invitation, never an authorization. |
| `audit_log` | Append-only forensic trail: actor, action, IP, user agent. |

### Why the ledger, not a balance column

Storing `balance` as truth and running `UPDATE ... SET balance = balance - 500`
loses history, cannot be audited, and is where lost-update bugs live. We append
immutable signed entries instead, keep `balance` as a projection updated in the
same transaction, and can therefore *prove* correctness with one query rather
than assert it in prose.

### Append-only is enforced, not agreed

`ledger_entries` and `audit_log` carry a `BEFORE UPDATE OR DELETE` trigger that
raises. Try it:

```sql
UPDATE ledger_entries SET amount = 999999;
-- ERROR:  ledger_entries is append-only: UPDATE is not permitted
```

Where a least-privilege `app_user` role exists, migration 0001 additionally
revokes `UPDATE` and `DELETE` on both tables.

---

## 5. Security

| Layer | Implementation |
|---|---|
| Input | Pydantic v2 schemas — invalid input never reaches business logic |
| Password | bcrypt, cost 12 |
| Transaction PIN | Separate bcrypt hash, required per transfer — a stolen session cannot move money |
| Token | JWT HS256 with the algorithm **whitelisted on decode**, 15-minute expiry |
| Revocation | `sessions` row; logout blocks it, refresh tokens rotate, reuse blocks the family |
| Authorization | Sender is derived from the token subject. `from_wallet_id` does not exist in any schema, so IDOR is closed by construction. |
| Injection | Parameterized queries only; no string-built SQL anywhere |
| Secrets | `.env` gitignored; the app refuses to boot on the placeholder `SECRET_KEY` |
| Audit | Every auth event and money movement, in an append-only table |
| Lockout | 5 failed logins → 15-minute block, recorded in the audit log |
| Enumeration | A wrong password and an unregistered number return an identical 401, and comparable response time |

Rate limiting and the live reconciliation endpoint arrive in Block 6.

### Refresh-token families

Rotation alone is not reuse detection. Each login starts a **family**; every
rotation inherits it. Replaying a spent token revokes the entire family in one
statement, so an attacker who steals a refresh token cannot keep working access
after the victim's next refresh trips the alarm. Migration `0002` adds
`sessions.family_id` for exactly this.

### Why the signup grant is a transaction

```
POST /api/auth/register  ->  grant_reference: "TXN3Y9XCBAR"
```

That grant debits `SYSTEM_MINT` and credits the new wallet. After three
registrations the mint holds `-300000.00` and the three users hold `+100000.00`
each — so `SUM(ledger_entries.amount)` is still exactly zero and every taka in
the system has a provable origin.

---

## 6. Scaling

Ten million users in three years is roughly 10,000 signups a day — a lot of
rows, not a lot of concurrent load. The ladder, in order:

1. **PgBouncer + multiple uvicorn workers.** The app is stateless, so this is configuration, not a rewrite.
2. **Read replicas** for statements, history and lookup — about 90% of traffic. The write path stays on the primary because it must stay transactional.
3. **Range-partition `ledger_entries` and `transactions` by month.** Queries are recency-biased; cold partitions archive out.
4. **Extract services in this order:** notifications → reporting → fraud scoring → ledger *last*, because the ledger is the thing that must remain one ACID unit. Extraction uses the transactional outbox pattern.
5. **Shard by `user_id`**, routed so both sides of a transfer usually land on the same shard; cross-shard transfers escalate to a saga with compensating reversal entries — which an append-only ledger supports natively.

### Deliberate omissions

- **No cache on balances.** A stale balance is an overdraft. We do not cache money.
- **No message queue.** At this scale the database transaction is the queue.
- **No microservices.** Splitting the ledger converts one ACID transaction into a distributed saga: more code, less safety.
- **No soft deletes on financial rows.** A correction is a `REVERSAL` transaction; history is never rewritten.

---

## 7. Project layout

```
app/
  config.py       settings; refuses to boot on the placeholder SECRET_KEY
  database.py     engine, session factory, declarative base
  constants.py    SYSTEM_MINT id, reference alphabet
  models/         users, sessions, wallets, transactions, entries, requests, audit
  schemas/        Pydantic contracts — the input security boundary
  services/       all money logic — no framework imports
    ledger_service.py   lock_wallets + post_double_entry: the only writer of money
    auth_service.py     register, login, token rotation, logout
  routers/        thin HTTP adapters
  core/           security primitives, dependencies, domain errors
  static/         frontend (Block 7)
migrations/       alembic; 0001 is the full schema as explicit SQL
tests/            concurrency, idempotency, invariants, IDOR (Block 4)
seed.py           reset + invariant checker
```

---

## 8. The transfer path

`app/services/transfer_service.py` is the file to read. Five guarantees, in
this order:

1. **Idempotency** — a retried request returns the original transaction. The mechanism is a partial unique index on `(initiated_by, idempotency_key)`, so a duplicate loses the race *in the database*, with no window between an application check and the write.
2. **Authorisation** — the PIN authorises the action, not merely the session.
3. **Lock order** — wallets are locked in ascending id, so A→B and B→A cannot deadlock.
4. **Lock, then read** — the balance is read after the lock is held, closing the lost-update race.
5. **Atomicity** — transaction row, two ledger entries, both balance updates and the audit row commit together or not at all.

Behind all five sits the database's own `CHECK (balance >= 0)`, which holds
even if every line of that file were wrong.

```
POST /api/transfers          Idempotency-Key: <uuid>
GET  /api/transfers/{ref}    only if you were a party to it
GET  /api/wallet/statement   keyset-paginated, signed amounts, running balance
```

## 9. Money requests

> *"My friend owes me ৳1,200. I want to collect it through the application."*

```
POST /api/requests                    ask someone to pay you
GET  /api/requests?box=incoming       what is waiting on you
POST /api/requests/{id}/approve       payer only, PIN required
POST /api/requests/{id}/decline       payer only
POST /api/requests/{id}/cancel        requester only
```

**A request is an invitation, never an authorization.** It moves no money and
grants no access to the payer's wallet. Only the payer can settle it, and only
by re-entering their PIN. The requester holding the id changes nothing — trying
to approve your own request returns 404, and there is a test for it.

Approval reuses `execute_transfer` unchanged, so a settled request gets exactly
the same guarantees as a direct send: ordered locks, lock-then-read, the
overdraft constraint, two immutable ledger entries.

**Three defences against paying twice:**

1. The request row is locked `FOR UPDATE` for the whole approval, so a second attempt waits and then finds the status is no longer `PENDING`.
2. The settlement carries the deterministic idempotency key `request:<id>`, so even if the lock were bypassed the partial unique index returns the first transaction instead of moving money again.
3. The transfer and the status change are **one** database transaction (`execute_transfer(..., commit=False)` inside a SAVEPOINT), so there is no window where the money has moved but the request still looks payable — and a failed settlement leaves the request payable later rather than consuming it.

Expiry is computed on read rather than swept by a job: there is no scheduler in
this system, and a request that looked pending but could not be paid would be a
lie to the user.

## 10. Testing

```bash
psql -U postgres -c "CREATE DATABASE money_test;"
pytest                                 # 44 passed, ~70s
pytest tests/test_concurrency.py -v    # the one to run for judges
```

Tests run against a **real PostgreSQL** database, never SQLite — SQLite
serialises writes at the file level, so the race test would pass there while
proving nothing. `conftest.py` refuses to run if `TEST_DATABASE_URL` does not
have `test` in the name.

| Suite | Tests | Covers |
|---|---|---|
| `test_concurrency.py` | 3 | Lost update, deadlock, concurrent idempotent retries |
| `test_transfer.py` | 12 | Entries, statement, pagination, idempotency, refusals, decimal precision |
| `test_security.py` | 11 | IDOR, cross-account reads, `alg:none`, enumeration, injection, ledger immutability |
| `test_requests.py` | 14 | Flow, ledger typing, self-approval, wrong PIN, strangers, wrong verb, double payment, 8 simultaneous approvals, expiry, failed settlement |
| `test_invariants.py` | 4 | The four invariants, including after 500 randomised operations |

**The suite paid for itself.** `test_no_double_spend_under_concurrency` caught a
real lost-update bug in our own locking code: the `SELECT ... FOR UPDATE` was
correct and PostgreSQL took the lock, but SQLAlchemy returned the wallet object
already cached in the Session's identity map, so the balance we read was stale.
Twenty of twenty transfers succeeded against a wallet that could fund ten. The
fix is `populate_existing=True`; full write-up in `docs/TEST_COMMANDS.md`.
Reading the SQL would never have found it.
