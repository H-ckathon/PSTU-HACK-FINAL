# Money Movement Application

**PSTU IT Carnival 2026 — Hackathon Challenge**

A closed money ecosystem with simulated funds. Money moves only through an
append-only double-entry ledger; every transfer is idempotent, every balance is
reconcilable against the ledger, and no user wallet can go negative.

> **Complete.** Backend, frontend, 165 tests against real PostgreSQL, and the
> supporting documents. One process, one database, one command.

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

Then open **http://127.0.0.1:8000** for the app, or
**http://127.0.0.1:8000/docs** for the API.

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

`transactions.reverses_transaction_id` links a `REVERSAL` to the transaction it
corrects, with a partial unique index so a transaction can be reversed at most
once — a database guarantee, not an application check.

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
| Refunds | Only the recipient can return money, and only with their PIN — a sender can never claw a payment back |
| Lockout | 5 failed logins → 15-minute block, recorded in the audit log |
| Rate limit | slowapi: 5/min login and register, 10/min transfers and requests, 20/min lookup |
| Enumeration | A wrong password and an unregistered number return an identical 401, and comparable response time |

### Rate limiting is keyed by account, not by IP

An IP-only limit is the wrong choice here. A whole office or campus can sit
behind one NAT address, so one busy user would throttle everyone around them.
Authenticated requests carry a subject, so money endpoints are limited **per
account**; only login and register fall back to IP, which is exactly where IP
is the right key.

Storage is in-process, which is correct for a single uvicorn process and adds
no dependency. With multiple workers each would keep its own counter — the fix
is a `storage_uri="redis://..."` on the `Limiter`, one line, no code change.
Naming that limit is better than pretending it is not there.

**Two mechanisms, failing differently on purpose.** Six wrong passwords produce
`401, 401, 401, 401, 423, 429`: the per-account lockout engages on the fifth
attempt and lasts fifteen minutes, and the per-key rate limit refuses the sixth
before it ever reaches bcrypt. An attacker rotating IP addresses still cannot
brute force a single account.

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
  static/         index.html — the whole frontend, one file, no build
migrations/       alembic; 0001 is the full schema as explicit SQL
tests/            concurrency, transfers, requests, security, invariants, limits
docs/             ARCHITECTURE.md · AI_PROMPTS.md · TEST_COMMANDS.md
seed.py           reset + demo users + invariant checker
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

## 9. Refunds — corrections without rewriting history

```
POST /api/transfers/{reference}/refund     recipient only, PIN required
```

Money movement is not always right the first time, so the system needs a way to
put a mistake right. It does **not** do that by editing or deleting anything —
it cannot, since `ledger_entries` is append-only at the database level. A refund
creates a **new** `REVERSAL` transaction with its own pair of signed entries,
pointing back at the transaction it corrects through
`transactions.reverses_transaction_id`.

The result is that the ledger holds both the mistake and the fix. The original
is marked `REVERSED` and its entries still say exactly what happened, which is
what an auditor actually wants: not a clean history, an honest one.

**Only the recipient can refund.** The original sender cannot claw a payment
back, because that would be precisely the pull-money-from-someone-else's-wallet
capability every other decision here exists to prevent. Refunding spends the
refunder's money, so it is authorised the way all spending is — with the PIN.

Three independent reasons a transaction cannot be refunded twice:

1. The original row is locked `FOR UPDATE` for the whole operation, so a second attempt waits and finds it already `REVERSED`.
2. A partial unique index, `uq_one_reversal_per_transaction`, means the **database** permits at most one reversal per transaction.
3. The settlement carries the deterministic key `reversal:<id>`.

Verified with eight simultaneous refunds: exactly one succeeds. A signup grant
cannot be refunded (there is nobody to return it to) and a reversal cannot
itself be reversed (corrections would chain without bound). If the refunder has
since spent the money the refusal is clean and the original stays refundable,
because the reversal and the status change share one transaction.

**Deliberately not built:** partial refunds, and a time window on refunds. A
partial refund needs a per-transaction refunded-total to stay correct under
concurrency; a time window is meaningful when there is external settlement to
race, and this is a closed ecosystem. Both are stated rather than silently
absent.

## 10. Money requests

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

## 11. Proving it, live

```
GET /api/admin/reconcile     asserts all four invariants against live data
GET /api/me/activity         your own audit trail
```

`reconcile` is the demo trump card. Run it at any moment:

```json
{
  "conservation": true, "balanced_events": true,
  "no_drift": true, "solvency": true, "all_hold": true,
  "ledger_sum": "0.00", "entry_count": 26, "offending": {}
}
```

When something is wrong it names the rows at fault in `offending`, so a failure
is a starting point rather than a red light. There is a test that plants a
deliberate inconsistency — tampering with a wallet balance directly, which the
ledger trigger cannot prevent because `wallets` is a projection — and asserts
that reconcile catches the drift and identifies the wallet. Otherwise a green
light would prove nothing.

`/api/me/activity` is scoped to the caller. There is no endpoint returning
anyone else's trail: an audit log readable by the wrong person is a
surveillance feature, not a security one.

## 12. The interface

One HTML file, served by the same process at `/`. **No build step, no
framework, no CDN** — nothing is fetched from anywhere, so the app renders with
the venue wifi down. The typeface is the operating system's own UI font, which
is why it looks native rather than like a web page imitating an app.

Five screens: sign in / register, home, send, request, approve. Amounts use
tabular figures so columns line up; money in is green, money out is red, and
that is the only colour in the interface.

Three details that are architecture, not decoration:

- **The recipient's name resolves before you pay.** Typing a number calls `/api/users/lookup`, which returns a name and never a balance — so you confirm who you are paying, and enumeration still reveals nothing financial.
- **The Send button is never disabled to prevent double submission.** One idempotency key is minted when the form opens, so a double click, a flaky network or an impatient tap returns the *original* transaction. Client-side disabling is not a guarantee; the server is. When a retry is recognised, the confirmation says so.
- **"Verify ledger"** runs `/api/admin/reconcile` and prints all four invariants in the interface, so the correctness argument is visible to anyone using the app, not just to whoever reads the tests.

Access tokens live in a JavaScript variable and are never written to
`localStorage`, so closing the tab ends the session and no token is left on disk.

## 13. Testing

```bash
psql -U postgres -c "CREATE DATABASE money_test;"
pytest                                 # 165 passed, ~4 min
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
| `test_refund.py` | 15 | Reversal entries, untouched originals, sender cannot claw back, double refund, spent money, eight simultaneous refunds |
| `test_race_conditions.py` | 11 | Three-way cycle, transfer vs. approval, mutual approvals, approve vs. decline, cancel vs. approve, logout mid-transfer, duplicate registration, contested refresh, 120 mixed operations |
| `test_edge_cases.py` | 85 | Hostile amounts, control characters, oversized inputs, malformed tokens, cursor tampering, bcrypt's 72-byte boundary |
| `test_rate_limit.py` | 10 | Throttling, per-account keying, lockout vs. rate limit precedence, reconcile (including a planted inconsistency), audit scoping |
| `test_invariants.py` | 4 | The four invariants, including after 500 randomised operations |

**The suite paid for itself.** `test_no_double_spend_under_concurrency` caught a
real lost-update bug in our own locking code: the `SELECT ... FOR UPDATE` was
correct and PostgreSQL took the lock, but SQLAlchemy returned the wallet object
already cached in the Session's identity map, so the balance we read was stale.
Twenty of twenty transfers succeeded against a wallet that could fund ten. The
fix is `populate_existing=True`; full write-up in `docs/TEST_COMMANDS.md`.
Reading the SQL would never have found it.

An adversarial pass afterwards — `test_edge_cases.py` and
`test_race_conditions.py`, written specifically to break the system — found six
more defects, including the same read-then-write race in the **sessions** table
(four concurrent refreshes of one token all succeeded), two inputs that
produced 500s on money endpoints, and `"1e3"` being silently accepted as ৳1,000.
All six are fixed and documented in `docs/TEST_COMMANDS.md`.

## 14. Demo script

Roughly three minutes. Reset first: `python seed.py --reset --demo`.

| # | Beat | The line |
|---|---|---|
| 1 | Start uvicorn, open the app and `/docs` | One process, one database, one command. |
| 2 | Sign in as Alice; open her statement | The opening balance is a `SIGNUP_GRANT` from the system mint, not a number in a column. Every taka has an origin. |
| 3 | Send ৳2,500 to Bob — the name resolves before paying, then the PIN | The PIN is separate from the password. A stolen session cannot move money. |
| 4 | **Double-click Send** | One transaction. The idempotency key means the retry returns the original — and notice we never disabled the button, because the guarantee is server-side. |
| 5 | Bob requests ৳1,200; Alice approves | A request is an invitation, never an authorization. Only the payer can settle it, with their own PIN. |
| 6 | Bob **returns** the ৳2,500 | Nothing is edited or deleted — the ledger keeps both the mistake and the fix, and the original shows struck through as returned. |
| 7 | Try to send ৳999,999 | Refused by a database `CHECK` constraint, not by the UI. Two independent layers. |
| 8 | `pytest tests/test_concurrency.py -v` | Twenty threads, ten succeed, ledger still sums to zero. |
| 9 | Click **Verify ledger** | All four invariants, asserted live. |
| 10 | Architecture: the three extraction seams | The ledger splits last, because it is the thing that must stay one ACID unit. |

Steps 4, 6, 7 and 8 are the ones worth protecting if you have to compress.

## 15. Further reading

- `docs/ARCHITECTURE.md` — the full design document
- `docs/AI_PROMPTS.md` — how AI was used, what we decided ourselves, and the three bugs it produced that we caught
- `docs/TEST_COMMANDS.md` — every test command and what each proves
