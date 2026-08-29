# Money Movement Build Spec

**PSTU IT Carnival 2026 — Hackathon Challenge**
Patuakhali Science and Technology University · 29 August 2026 · 9:00 AM – 3:00 PM

> A closed-ecosystem transfer platform, correct under concurrency, defensible to judges,
> and buildable by one team in a single day.

| | |
|---|---|
| **Runtime** | FastAPI · Python 3.11 |
| **Store** | PostgreSQL 16, local service |
| **Core model** | Double-entry ledger |
| **Demo** | One process, port 8000 |
| **Code freeze** | 2:15 PM |

---

## Table of contents

1. [The decision, stated once](#1-the-decision-stated-once)
2. [Final stack](#2-final-stack)
3. [Topology & repository](#3-topology--repository)
4. [Data model](#4-data-model)
5. [The four invariants](#5-the-four-invariants)
6. [API surface](#6-api-surface)
7. [The transfer path](#7-the-transfer-path)
8. [Security, layer by layer](#8-security-layer-by-layer)
9. [Tests — the proof](#9-tests--the-proof)
10. [Scaling ladder](#10-scaling-ladder)
11. [Build timeline](#11-build-timeline)
12. [The three-minute demo](#12-the-three-minute-demo)
13. [Judge questions, pre-answered](#13-judge-questions-pre-answered)
14. [What could go wrong today](#14-what-could-go-wrong-today)

---

## 1. The decision, stated once

> [!IMPORTANT]
> **Thesis —** A modular monolith over PostgreSQL, where money moves only through an
> append-only double-entry ledger, every write is idempotent, and every module boundary
> is a seam that can be cut into a service later.

The rubric rewards *trustworthiness under stress*, not feature count. So the architecture
spends its complexity budget in exactly one place — the transfer path — and stays
deliberately plain everywhere else. Every remaining decision is either a correctness
guarantee you can demonstrate live, or a removal you can defend as restraint.

### What the simplebank reference confirmed — and where we go further

The `techschool/simplebank` project (Go, sqlc, gRPC, asynq, EKS) is a multi-week course
build, not a six-hour one. Its **shape** validates ours; its **scope** is the trap.

| Simplebank does | We do | Why |
|---|---|---|
| Tables: accounts, entries, transfers | Same three, renamed `wallets`, `ledger_entries`, `transactions` | Independent convergence on the same model is itself an argument. Say so. |
| Locks accounts in a fixed ID order to avoid deadlock | Identical — sorted lock acquisition | This is the canonical solution. Adopt it without apology. |
| Stores `balance` on the account as source of truth | Ledger is truth; `balance` is a projection updated in the same transaction | **Our upgrade.** Enables the sum-to-zero invariant they cannot assert. |
| Entries are an audit trail alongside the balance update | Entries are *signed* and must sum to zero, globally and per transaction | Turns "audit trail" into a provable invariant you run live. |
| Sessions table for refresh-token rotation | Adopted — `sessions` table with `is_blocked` | Closes the stateless-JWT revocation gap for ~20 min of work. |
| PASETO preferred over JWT | JWT, HS256, algorithm whitelisted | PASETO is genuinely better; JWT with an explicit whitelist closes the same hole and ships faster. Name the trade-off before a judge does. |
| Redis + asynq worker, gRPC gateway, Docker, EKS, CI/CD | None of it | Six hours. These live in the README as the scaling ladder, not in the repo. |
| No idempotency keys on transfers | Partial unique index on `(initiated_by, idempotency_key)` | **Our upgrade.** The most visible correctness feature in the demo. |
| No overdraft constraint at the database level | `CHECK (balance >= 0)` | **Our upgrade.** Two independent layers guard one invariant. |

> **Line to say:** *"The reference implementations of this problem all converge on
> double-entry with ordered locking. We built that, then added the three things production
> money systems have and tutorials usually skip: idempotency keys, a database-enforced
> overdraft constraint, and a ledger invariant we can assert on demand."*

---

## 2. Final stack

Every row carries the sentence you say when a judge points at it.

| Component | Choice | Defense line | |
|---|---|---|---|
| Language | `Python 3.11` | Team's fastest path to *correct* code, which is the whole rubric | core |
| Framework | `FastAPI` | Schema validation at the edge, and `/docs` is a free demo surface | core |
| Validation | `Pydantic v2` | Bad input dies at the schema layer, never inside money logic | core |
| ORM / driver | `SQLAlchemy 2.x sync + psycopg2` | `with_for_update()` is unambiguous; sync avoids async session traps under time pressure | core |
| Database | `PostgreSQL 16, local service` | ACID, row locks, CHECK constraints, partitioning when we need it | core |
| Migrations | `Alembic` | Schema is versioned and reproducible on any machine in one command | core |
| Money type | `Decimal / NUMERIC(15,2)` | No floating point anywhere in financial code | core |
| Overdraft | `CHECK (balance >= 0)` | The database physically cannot hold a negative user balance | **ADDED** |
| Concurrency | `SELECT … FOR UPDATE`, ascending wallet id | Lock before read closes the lost-update race; sorted order makes deadlock impossible | core |
| Idempotency | Header + partial unique index | A retried request returns the original transaction. Money moves once. | core |
| Ledger | Signed entries, append-only, `REVOKE`'d | Immutability is a database permission, not a team convention | core |
| Auth | bcrypt + JWT HS256 (whitelisted) + sessions table | Whitelist closes `alg:none`; the sessions row gives us real revocation | **ADDED** |
| Transaction PIN | Separate bcrypt hash, required per transfer | A stolen session alone cannot move money | **ADDED** |
| Rate limiting | `slowapi`, in-process | Credential stuffing and transfer spam throttled at the edge; swaps to Redis unchanged | **ADDED** |
| Requests feature | `MoneyRequest` state machine | The brief names it explicitly. Half the product otherwise. | **ADDED** |
| Frontend | Static HTML + fetch + Tailwind CDN via `StaticFiles` | One server, one origin, no build step, no CORS | core |
| Tests | `pytest` + threaded race test | Proof, not promises | core |
| Cache | **none** | A stale balance is an overdraft. We deliberately do not cache money. | cut |
| Queue / worker | **none** | At this scale the database transaction *is* the queue | cut |
| Docker | **none** | Not required; a local service removes the largest demo-day failure mode | cut |
| Microservices | **none** | Splitting the ledger converts one ACID transaction into a distributed saga | cut |

> [!NOTE]
> **Frontend note —** React (Vite → build into `app/static/`) is a legitimate upgrade *only*
> with a dedicated person and a 90-minute hard timebox. Flutter is the wrong tool today:
> multi-megabyte web bundles, slow rebuild loops, and a white-screen failure mode on a
> projector. The architecture is identical either way — same origin, served by FastAPI.

---

## 3. Topology & repository

One process, one database, one command. The seams are visible in the folder tree.

```text
┌──────────────────────────────────────────────────┐
│  Browser — static index.html + fetch + Tailwind   │
└────────────────────────┬─────────────────────────┘
                         │ HTTP/JSON · Bearer JWT · Idempotency-Key
┌────────────────────────▼─────────────────────────┐
│  uvicorn → FastAPI   (single process, :8000)      │
│                                                   │
│  middleware   rate limit → auth → audit           │
│  routers/     thin — HTTP in, HTTP out            │
│  services/    business rules + the DB transaction │
│  models/      SQLAlchemy ORM                      │
│  StaticFiles  mounted at "/"                      │
└────────────────────────┬─────────────────────────┘
                         │ psycopg2 (sync)
┌────────────────────────▼─────────────────────────┐
│  PostgreSQL 16 — local service                    │
│  users · sessions · wallets · transactions        │
│  ledger_entries · money_requests · audit_log      │
│  ← invariants live HERE, not in Python            │
└──────────────────────────────────────────────────┘
```

```text
money-movement/
├── app/
│   ├── main.py              # app, middleware, StaticFiles mount
│   ├── config.py            # pydantic-settings; refuses default SECRET_KEY
│   ├── database.py          # engine, SessionLocal, Base, get_db()
│   ├── models/              # user, session, wallet, transaction, request, audit
│   ├── schemas/             # Pydantic v2 request/response contracts
│   ├── services/            # ← ALL money logic. No framework imports.
│   │   ├── auth_service.py
│   │   ├── ledger_service.py     # the only writer of ledger_entries
│   │   ├── transfer_service.py   # the file judges will read
│   │   └── request_service.py
│   ├── routers/             # auth, wallet, transfers, requests, admin
│   ├── core/                # security, deps, limiter, errors
│   └── static/index.html
├── migrations/              # alembic
├── tests/
│   ├── test_transfer.py
│   ├── test_concurrency.py  # ← the one that wins
│   ├── test_invariants.py
│   └── test_security.py
├── docs/  AI_PROMPTS.md · TEST_COMMANDS.md · ARCHITECTURE.md
├── seed.py · .env.example · README.md
```

**Why `services/` is a separate layer:** it imports no HTTP and no framework. When a judge
asks how this becomes a service, the answer is physical — that file lifts out unchanged, and
`routers/` is the disposable adapter. Module boundaries are the scaling story; the folder
tree is the evidence.

---

## 4. Data model

Seven tables. Every guarantee that matters is expressed as a constraint, so correctness
survives a bug in the application layer.

### Identity & sessions

```sql
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone         VARCHAR(20)  NOT NULL UNIQUE,      -- identity users recognise
    full_name     VARCHAR(100) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,             -- bcrypt, cost 12
    pin_hash      VARCHAR(255) NOT NULL,             -- separate 4-digit txn PIN
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- adopted from simplebank: gives stateless JWT a real revocation path
CREATE TABLE sessions (
    id            UUID PRIMARY KEY,                    -- = refresh token jti
    user_id       UUID NOT NULL REFERENCES users(id),
    refresh_hash  VARCHAR(255) NOT NULL,
    user_agent    VARCHAR(255),
    client_ip     INET,
    is_blocked    BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Wallets — where the overdraft guarantee lives

```sql
CREATE TYPE wallet_type AS ENUM ('USER', 'SYSTEM');

CREATE TABLE wallets (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID UNIQUE REFERENCES users(id),    -- NULL for system wallets
    type       wallet_type   NOT NULL DEFAULT 'USER',
    currency   CHAR(3)       NOT NULL DEFAULT 'BDT',
    balance    NUMERIC(15,2) NOT NULL DEFAULT 0,    -- projection, not truth
    updated_at TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT wallet_owner CHECK (
        (type = 'USER'   AND user_id IS NOT NULL) OR
        (type = 'SYSTEM' AND user_id IS NULL)
    ),
    CONSTRAINT no_overdraft CHECK (
        type = 'SYSTEM' OR balance >= 0             -- ← the money guarantee
    )
);
```

The `SYSTEM_MINT` wallet is exempt from the overdraft check because it goes negative
**by design**. It is where every ৳100,000 signup grant originates, so every taka in the
system has a provable source and the global ledger still sums to zero.

### Transactions — one row per business event

```sql
CREATE TYPE txn_type   AS ENUM ('SIGNUP_GRANT','TRANSFER','REQUEST_SETTLEMENT','REVERSAL');
CREATE TYPE txn_status AS ENUM ('PENDING','COMPLETED','FAILED','REVERSED');

CREATE TABLE transactions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reference          VARCHAR(20)   NOT NULL UNIQUE,   -- 'TXN8H2K4M9P'
    type               txn_type      NOT NULL,
    status             txn_status    NOT NULL DEFAULT 'PENDING',
    amount             NUMERIC(15,2) NOT NULL CHECK (amount > 0),
    sender_wallet_id   UUID REFERENCES wallets(id),
    receiver_wallet_id UUID REFERENCES wallets(id),
    note               VARCHAR(255),
    idempotency_key    VARCHAR(64),
    initiated_by       UUID REFERENCES users(id),
    failure_reason     VARCHAR(100),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ,

    CONSTRAINT no_self_transfer CHECK (sender_wallet_id <> receiver_wallet_id)
);

-- THIS index is the idempotency mechanism. No extra table, no race window.
CREATE UNIQUE INDEX uq_idem ON transactions (initiated_by, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
```

### Ledger entries — append-only, the source of truth

```sql
CREATE TABLE ledger_entries (
    id             BIGSERIAL PRIMARY KEY,
    transaction_id UUID NOT NULL REFERENCES transactions(id),
    wallet_id      UUID NOT NULL REFERENCES wallets(id),
    amount         NUMERIC(15,2) NOT NULL CHECK (amount <> 0),  -- signed
    balance_after  NUMERIC(15,2) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    direction      VARCHAR(6) GENERATED ALWAYS AS
                   (CASE WHEN amount < 0 THEN 'DEBIT' ELSE 'CREDIT' END) STORED
);

CREATE INDEX idx_entries_wallet ON ledger_entries (wallet_id, created_at DESC, id DESC);
CREATE INDEX idx_entries_txn    ON ledger_entries (transaction_id);

-- Immutability as a database permission, not a team convention:
REVOKE UPDATE, DELETE ON ledger_entries, audit_log FROM app_user;
```

Signed amounts mean the correctness proof is a single query. `balance_after` renders
statements without recomputation and makes tampering detectable by replaying the chain.

### Money requests & audit log

```sql
CREATE TYPE request_status AS ENUM ('PENDING','APPROVED','DECLINED','CANCELLED','EXPIRED');

CREATE TABLE money_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_id   UUID NOT NULL REFERENCES users(id),   -- wants the money
    payer_id       UUID NOT NULL REFERENCES users(id),   -- must approve
    amount         NUMERIC(15,2) NOT NULL CHECK (amount > 0),
    note           VARCHAR(255),
    status         request_status NOT NULL DEFAULT 'PENDING',
    transaction_id UUID REFERENCES transactions(id),     -- set on approval
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    responded_at   TIMESTAMPTZ,

    CONSTRAINT no_self_request CHECK (requester_id <> payer_id)
);

CREATE TABLE audit_log (
    id            BIGSERIAL PRIMARY KEY,
    actor_user_id UUID REFERENCES users(id),
    action        VARCHAR(50) NOT NULL,   -- LOGIN_FAILED, TRANSFER_COMPLETED, …
    entity_type   VARCHAR(50),
    entity_id     UUID,
    ip_address    INET,
    user_agent    VARCHAR(255),
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## 5. The four invariants

Put these on a slide. They are the entire correctness argument, and
`GET /api/admin/reconcile` asserts all four on demand.

| # | Name | Invariant | Meaning |
|---|---|---|---|
| 01 | Conservation | `Σ amount = 0` | Across every entry ever written. Money is never created or destroyed. |
| 02 | Balanced events | `Σ per txn = 0` | Every business event debits exactly what it credits. |
| 03 | No drift | `balance = Σ entries` | The projection always agrees with the ledger. |
| 04 | Solvency | `balance ≥ 0` | For every USER wallet. Enforced by CHECK, not by code. |

```sql
-- the whole proof, three queries
SELECT SUM(amount) FROM ledger_entries;                        -- 0.00

SELECT transaction_id FROM ledger_entries
 GROUP BY transaction_id HAVING SUM(amount) <> 0;              -- 0 rows

SELECT w.id FROM wallets w
  LEFT JOIN ledger_entries e ON e.wallet_id = w.id
 GROUP BY w.id, w.balance
HAVING w.balance <> COALESCE(SUM(e.amount), 0);                -- 0 rows
```

---

## 6. API surface

Fifteen endpoints. Nothing speculative.

| Method | Path | Purpose | Guard |
|---|---|---|---|
| `POST` | `/api/auth/register` | User + wallet + ৳100,000 grant, one atomic transaction | 5/min |
| `POST` | `/api/auth/login` | Phone + password → access JWT + refresh token | 5/min · lockout |
| `POST` | `/api/auth/refresh` | Rotate refresh token; reuse blocks the session | 10/min |
| `POST` | `/api/auth/logout` | Sets `sessions.is_blocked` | auth |
| `GET` | `/api/me` | Profile + balance | auth |
| `GET` | `/api/wallet/statement` | Ledger entries, keyset-paginated | auth |
| `POST` | `/api/transfers` | Send money — `Idempotency-Key` header required | auth · PIN · 10/min |
| `GET` | `/api/transfers/{ref}` | Single transaction detail | auth · party only |
| `POST` | `/api/requests` | Ask another user for money | auth · 10/min |
| `GET` | `/api/requests` | `?box=incoming\|outgoing` | auth |
| `POST` | `/api/requests/{id}/approve` | Payer approves → creates the transfer | auth · PIN · payer only |
| `POST` | `/api/requests/{id}/decline` | Payer declines | auth · payer only |
| `POST` | `/api/requests/{id}/cancel` | Requester withdraws | auth · requester only |
| `GET` | `/api/users/lookup` | `?phone=` → full name only, never balance | auth · 20/min |
| `GET` | `/api/admin/reconcile` | Live invariant check — the demo trump card | auth |

> [!TIP]
> **Keyset, not OFFSET —**
> `WHERE (created_at, id) < (:cursor_ts, :cursor_id) ORDER BY created_at DESC, id DESC LIMIT 20`
>
> `OFFSET 500000` forces a scan; keyset stays an index seek at any depth. A scalability
> answer embedded in working code rather than a claim in a README.

---

## 7. The transfer path

This is the file judges will read. Every line has a reason, and the four defense lines
follow it.

```python
def execute_transfer(db, sender, recipient_phone, amount, pin, idem_key, note):

    # ── OUTSIDE the transaction ─────────────────────────────────
    # 1. Idempotency replay — a lookup, no lock needed
    existing = db.query(Transaction).filter_by(
        initiated_by=sender.id, idempotency_key=idem_key).one_or_none()
    if existing:
        return existing                      # same result, money moved once

    # 2. Authenticate the ACTION, not merely the session
    if not verify_pin(pin, sender.pin_hash):
        audit('PIN_FAILED', sender); raise InvalidPin()

    recipient = db.query(User).filter_by(phone=recipient_phone).one_or_none()
    if not recipient or not recipient.is_active: raise RecipientNotFound()
    if recipient.id == sender.id:              raise SelfTransferNotAllowed()

    # ── INSIDE ONE ACID TRANSACTION ─────────────────────────────
    try:
        # 3. Deterministic lock order → deadlock is structurally impossible
        ids = sorted([sender.wallet.id, recipient.wallet.id])
        locked = (db.query(Wallet).filter(Wallet.id.in_(ids))
                    .order_by(Wallet.id).with_for_update().all())
        w = {x.id: x for x in locked}
        src, dst = w[sender.wallet.id], w[recipient.wallet.id]

        # 4. Balance read AFTER the lock — this is the entire point
        if src.balance < amount:
            raise InsufficientFunds(available=src.balance)

        txn = Transaction(reference=make_ref(), type='TRANSFER',
                          status='PENDING', amount=amount,
                          sender_wallet_id=src.id, receiver_wallet_id=dst.id,
                          note=note, idempotency_key=idem_key,
                          initiated_by=sender.id)
        db.add(txn); db.flush()

        # 5. Two signed entries, summing to zero
        src.balance -= amount        # CHECK constraint is the backstop
        dst.balance += amount
        db.add_all([
            LedgerEntry(transaction_id=txn.id, wallet_id=src.id,
                        amount=-amount, balance_after=src.balance),
            LedgerEntry(transaction_id=txn.id, wallet_id=dst.id,
                        amount=+amount, balance_after=dst.balance),
        ])

        txn.status, txn.completed_at = 'COMPLETED', func.now()
        db.add(AuditLog(actor_user_id=sender.id, action='TRANSFER_COMPLETED',
                        entity_type='transaction', entity_id=txn.id,
                        metadata={'amount': str(amount)}))
        db.commit()                  # ← atomic: all of it, or none of it
        return txn

    except IntegrityError as e:
        db.rollback()
        if 'uq_idem' in str(e.orig):      # concurrent duplicate won the race
            return db.query(Transaction).filter_by(
                initiated_by=sender.id, idempotency_key=idem_key).one()
        if 'no_overdraft' in str(e.orig):
            raise InsufficientFunds()
        raise
```

### The four defense lines

> **Defense 1 · lost update** — *"Read-then-write on a balance is the classic lost-update
> bug. We take the row lock **before** reading the balance, so the check and the write are
> one atomic step."*

> **Defense 2 · deadlock** — *"A→B and B→A running concurrently deadlock under naive
> locking. We sort the wallet IDs, so every transaction in the system acquires locks in the
> same global order. Deadlock cannot occur."*

> **Defense 3 · defence in depth** — *"If our Python check were wrong, the CHECK constraint
> still rejects the write. Two independent layers guard the same invariant."*

> **Defense 4 · retries** — *"A retried request returns the original transaction. Money
> moves exactly once, whatever the network does."*

### Registration uses the same path

The signup grant is a `SIGNUP_GRANT` transaction debiting `SYSTEM_MINT` and crediting the
new wallet — never `INSERT INTO wallets (balance) VALUES (100000)`. Every taka has an origin
entry and invariant 01 holds from the first user onward.

### Approving a request

`SELECT … FOR UPDATE` on the request row, assert `status = 'PENDING'` and
`expires_at > now()`, call `execute_transfer()` unchanged, then set `APPROVED` and link
`transaction_id` — all in one transaction, so double-approval is impossible.

> **Line to say:** *"A request is an invitation, never an authorization. Only the payer can
> initiate a debit, and the payer re-enters their PIN to do it."*

---

## 8. Security, layer by layer

Eleven layers, each cheap, each with a one-sentence defense.

| Layer | Implementation | Line to say |
|---|---|---|
| Input | Pydantic v2 — `amount: Field(gt=0, max_digits=15, decimal_places=2)`, phone regex | Invalid input never reaches business logic |
| Rate limit | slowapi — 5/min login, 10/min transfer, 30/min global | Credential stuffing and transfer spam are throttled at the edge |
| Lockout | 5 failed logins → 15-min block, logged | Brute force is bounded, and every attempt is in the audit trail |
| Password | bcrypt cost 12 | Slow by design; the cost factor is tunable as hardware improves |
| Transaction PIN | Separate bcrypt hash, required per transfer and per approval | Session compromise alone cannot move money |
| Token | JWT HS256, `algorithms=["HS256"]` on decode, 15-min expiry, jti + sub | The whitelist closes the `alg:none` forgery attack |
| Revocation | sessions row, rotating refresh token, reuse → block family | Stateless access tokens, stateful revocation. Logout is real. |
| Authorization | Sender derived from `token.sub`. `from_wallet_id` does not exist in any schema. | You cannot spend from an account you hold no token for — IDOR is closed by construction |
| Data exposure | Lookup returns name only; balances never appear in another user's response | Phone enumeration reveals nothing financial |
| Injection | SQLAlchemy parameterized queries; zero raw f-string SQL | No string-concatenated SQL anywhere in the codebase |
| Secrets | `.env` gitignored, `.env.example` committed, boot fails on default `SECRET_KEY` | The app fails loudly rather than running insecurely |
| Audit | Every auth event and money movement: actor, IP, UA, JSONB metadata | Full forensic trail, and the table is `REVOKE`'d from UPDATE and DELETE |

> [!WARNING]
> **Check before you submit —** `git log -p | grep -i -E "secret|password|api_key"`
> A committed secret in the repo is an instant credibility loss, and it is the single most
> common self-inflicted wound at hackathons.

---

## 9. Tests — the proof

Four files. One of them wins the contest.

```python
# tests/test_concurrency.py  ← run this live for the judges
def test_no_double_spend_under_concurrency():
    """Alice has 1000. Twenty threads each send 100. Exactly ten succeed."""
    alice, bob = seed_users(alice_balance=Decimal("1000.00"))

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: try_transfer(alice, bob, "100.00"), range(20)))

    assert sum(r.ok for r in results) == 10
    assert balance(alice) == Decimal("0.00")
    assert balance(bob)   == Decimal("1000.00")
    assert ledger_sum()   == Decimal("0.00")      # invariant 01 survived


def test_idempotent_retry_charges_once():
    key = str(uuid4())
    t1 = transfer(alice, bob, "500.00", idem_key=key)
    t2 = transfer(alice, bob, "500.00", idem_key=key)
    assert t1.reference == t2.reference
    assert balance(alice) == Decimal("99500.00")


def test_cannot_spend_from_another_users_wallet():
    r = client.post("/api/transfers",
                    json={"from_wallet_id": mallory_wallet, ...},
                    headers=auth(alice))
    assert r.status_code == 422    # the field does not exist in the schema


def test_invariants_hold_after_random_burst():
    run_random_transfers(n=500)
    for check in (conservation, per_txn_balance, no_drift, solvency):
        assert check() is True
```

> [!CAUTION]
> **The threading test only works against a real database.** Run pytest against a
> `money_test` PostgreSQL database, not SQLite — SQLite's locking model will give you a
> passing test that proves nothing, and a judge who spots that has found your one soft spot.

---

## 10. Scaling ladder

Framed as *"here is the ceiling we hit, and here is the next move."* Ten million users in
three years is roughly 10,000 signups a day — a lot of rows, not a lot of concurrent load.
Say that first; over-engineering is its own failure mode.

| Scale | Bottleneck | Move |
|---|---|---|
| **0 – 100k** | none | Current single process. One Postgres box sustains thousands of TPS. |
| **100k – 1M** | Connection exhaustion | **PgBouncer** in transaction pooling mode, plus multiple uvicorn workers behind nginx. The app is stateless — JWT, no in-process session — so this is configuration, not a rewrite. |
| **1M – 5M** | Read load on the primary | **Read replicas** for statements, history and lookup — roughly 90% of traffic. The write path stays on the primary because it must stay transactional. |
| **1M – 5M** | Unbounded ledger growth | **Range-partition `ledger_entries` and `transactions` by month.** Queries are recency-biased; cold partitions archive out. |
| **5M – 10M** | Slow non-critical work in the write path | **Extract in this order:** notifications → statements and reporting → fraud scoring → ledger *last*, precisely because the ledger is the thing that must remain one ACID unit. Extraction uses the **transactional outbox** so side effects stay consistent with committed money. |
| **10M+** | Single-primary write ceiling | **Shard by `user_id`**, routed so both sides of a transfer usually land on the same shard. Cross-shard transfers escalate to a two-phase saga with compensating reversal entries — which an append-only ledger supports natively, since a reversal is a new pair of entries rather than an edit. |

### Deliberate non-decisions

List these out loud. Restraint that is *named* reads as maturity; restraint that is silent
reads as an oversight.

- **No cache on balances.** A stale balance is an overdraft. We do not cache money.
- **No message queue.** At this scale the database transaction is the queue. Kafka would add failure modes without removing any.
- **No microservices.** Splitting the ledger converts one ACID transaction into a distributed saga — strictly more code, strictly less safety. The module boundaries already exist for when it is warranted.
- **No Docker.** The judging environment is our machine. A local service removes a build-and-pull step from the riskiest five minutes of the day.
- **No soft deletes on financial rows.** Financial history is never rewritten. A correction is a `REVERSAL` transaction.

---

## 11. Build timeline

Nine to three, with a gate on every block. A block that misses its gate does not roll
forward — it gets cut.

### 9:00 — Skeleton, models, migration, seed
Repo scaffold, all seven tables, Alembic migration applied, `seed.py` creating
`SYSTEM_MINT`. One person on models, one on `index.html` shell, one on the auth router.
> **Gate —** `alembic upgrade head` clean, ledger sums to zero on seed data

### 10:00 — Auth and registration
bcrypt, JWT, sessions table, `get_current_user` dependency. Registration issues a
`SIGNUP_GRANT` transaction — not a bare balance write.
> **Gate —** register two users, both show ৳100,000, mint wallet is −৳200,000

### 11:00 — The transfer path
`transfer_service.execute_transfer()` in full: idempotency, PIN, ordered locks, two entries,
audit row. Then `POST /api/transfers` and the statement endpoint.
> **Gate —** a transfer works end to end through `/docs`

### 12:00 — Tests (the hard stop)
Concurrency, idempotency, invariants, IDOR. This is the block nobody may skip. If the race
test fails, everything else waits.
> **Gate —** all four test files green against real PostgreSQL

### 12:45 — Money requests
Model, three endpoints, approval calling `execute_transfer()` unchanged. Roughly 45 minutes
because everything is reused.
> **Gate —** request → approve → money moves, request links its transaction

### 1:30 — Hardening and frontend
Rate limits, audit middleware, `/api/admin/reconcile`. In parallel: the five screens —
auth, home, send, requests, history.
> **Gate —** full flow demoable in a browser with no console errors

### 2:15 — Code freeze
README with all seven sections, `AI_PROMPTS.md`, `TEST_COMMANDS.md`. Reset the database,
reseed, rehearse the demo twice with a timer. Assign who speaks to which section.
> **Gate —** two clean rehearsals under three minutes

### 3:00 — Present
Fresh database, fresh terminal, browser and `/docs` tabs already open, laptop on mains
power, notifications off.

> [!WARNING]
> **Cut order, if you fall behind —** drop in this order:
> **frontend polish → audit log → rate limiting → money requests.**
> Never cut the concurrency test or the ledger. A correct transfer with a plain frontend
> beats a beautiful app that double-spends, and the 12:00 block is what proves the
> difference.

---

## 12. The three-minute demo

Steps 4, 6 and 7 are the ones other teams will not have. Everything else is setup for them.

| # | Beat | What you say | Time |
|---|---|---|---|
| 1 | Start uvicorn, open `/docs` | One process, one database, one command. | 15s |
| 2 | Register Alice and Bob; show the `SIGNUP_GRANT` entries and the negative mint wallet | Every taka has an origin. Nothing is conjured into a balance column. | 30s |
| 3 | Alice sends Bob ৳2,500 with her PIN | The PIN is separate from the login password — a stolen session cannot move money. | 20s |
| 4 | **Double-click Send.** Show one transaction and the repeated key in the network tab | Idempotency key. The retry returns the original transaction. | 20s |
| 5 | Bob requests ৳1,200; Alice approves | A request is an invitation, never an authorization. | 25s |
| 6 | Alice tries to send ৳999,999 — show it is the **CHECK constraint** rejecting it, not the UI | Even with a bug in our code, the database cannot hold a negative balance. | 20s |
| 7 | Run `pytest tests/test_concurrency.py -v` live | Twenty threads, ten succeed, ledger still sums to zero. | 30s |
| 8 | `GET /api/admin/reconcile` | All four invariants, asserted on demand. | 10s |
| 9 | Architecture slide — three extraction seams, the scaling ladder | Here is where this splits when it needs to, and why the ledger splits last. | 20s |

### Demo-day hygiene

- `make demo` resets and reseeds in under 20 seconds. Rehearse the recovery, not just the happy path.
- Bind `0.0.0.0` and have it open on a phone over your hotspot — a free wow moment if the room allows it.
- Terminal font at 16pt or larger. Judges are reading a projector, not your screen.
- Notifications off, battery on mains, browser tabs pre-opened, second terminal already in the repo.
- One person drives, one narrates. Never both on the keyboard.

---

## 13. Judge questions, pre-answered

The ones that get asked. Rehearse the first four out loud.

<details>
<summary><b>Why a monolith? Wouldn't microservices scale better?</b></summary>

They would scale the parts that need it, and we have named the extraction order for exactly
that. But a transfer spanning an accounts service and a ledger service is no longer one ACID
transaction — it becomes a saga with compensating actions, which is strictly more code and
strictly more ways to lose money. We kept the money path atomic and made the boundaries
explicit so extraction is a refactor, not a rewrite.
</details>

<details>
<summary><b>What happens if the server crashes mid-transfer?</b></summary>

Nothing partial can survive. The two ledger entries, the balance updates, the transaction
row and the audit row are one Postgres transaction — if the process dies before commit,
Postgres rolls all of it back and the ledger is unchanged. The client retries with the same
idempotency key and either gets the completed transaction or a fresh attempt. There is no
state where one wallet was debited and the other was not.
</details>

<details>
<summary><b>Two people send money to each other at the exact same moment. What happens?</b></summary>

Naively that deadlocks — each transaction holds the lock the other needs. We sort wallet IDs
before acquiring locks, so every transaction in the system takes locks in the same global
order and a cycle cannot form. One transaction waits for the other and both complete. This
is demonstrated by the concurrency test.
</details>

<details>
<summary><b>How do you know balances are correct?</b></summary>

We do not store balances as truth. Truth is the append-only entry ledger, where every entry
is signed and the sum across the whole system must be exactly zero. The balance column is a
projection updated inside the same transaction, and `/api/admin/reconcile` proves the
projection still agrees with the ledger. Run it any time you like.
</details>

<details>
<summary><b>Why NUMERIC instead of integers or floats?</b></summary>

Floats are disqualifying — `0.1 + 0.2` is not `0.3`, and in money that is a defect.
`NUMERIC(15,2)` gives exact decimal arithmetic in the database and maps to Python's
`Decimal` with no conversion layer. Integer minor units also work; we chose NUMERIC because
it removes a class of unit-conversion bugs at the API boundary and it is readable in psql
during a demo.
</details>

<details>
<summary><b>Your JWT is stateless — how do you log someone out?</b></summary>

The access token is deliberately short-lived, fifteen minutes. Long-lived state sits in the
sessions table: logout blocks the session, and refresh tokens rotate, so a replayed refresh
token blocks the whole family. That is the standard trade — stateless verification on the
hot path, stateful revocation on the cold one. PASETO would be a marginal improvement over
JWT here; we whitelisted the algorithm on decode, which closes the specific hole JWT is
criticised for.
</details>

<details>
<summary><b>What stops me spending from someone else's wallet?</b></summary>

The API has no field for it. The sender is derived from the token subject on the server;
there is no `from_wallet_id` in any request schema, so the most common API vulnerability
class is closed by construction rather than by a check we might forget. There is a test
asserting that.
</details>

<details>
<summary><b>How would you detect fraud?</b></summary>

Today, velocity rules on top of the audit log — transaction count and total amount per user
per window, flagged for hold. The architecture matters more than the rules: because every
movement is an immutable entry with an actor, an IP and a timestamp, a scoring service can
be added as a read-side consumer without touching the write path. That is the third
extraction in our ladder.
</details>

<details>
<summary><b>Why no caching or queue? Isn't that a scaling weakness?</b></summary>

It is a deliberate omission with a stated trigger. Caching a balance risks serving a stale
one, and a stale balance is an overdraft; we cache nothing on the money path by choice. A
queue would matter once notifications and reporting move off the write path, which is
precisely when we introduce the transactional outbox. Adding either today would add failure
modes without removing one.
</details>

<details>
<summary><b>What is the weakest part of this system?</b></summary>

Single Postgres primary — one machine is the write ceiling and a single point of failure. In
production the first two moves are a synchronous standby with automatic failover and
PgBouncer in front. Second weakest is that we have velocity limits but no real fraud
scoring; the hooks are there and the ledger makes it straightforward, but we did not build
it in six hours.

*Answering this honestly scores better than claiming there is no weakness. Every judge knows
there is one.*
</details>

<details>
<summary><b>You used AI to build this. What did you actually decide?</b></summary>

Point at the specifics: the signed-ledger model over a plain balance column, the sorted lock
ordering, the partial unique index as the idempotency mechanism, the CHECK constraint as a
second layer, the deliberate omissions and their triggers. Then walk them through
`transfer_service.py` line by line. `docs/AI_PROMPTS.md` documents the process; the ability
to defend every line is the thing being graded.
</details>

---

## 14. What could go wrong today

Each of these has cost a hackathon team a placing. Each takes two minutes to prevent.

| Risk | Prevention |
|---|---|
| Postgres not running, or wrong credentials, at 3:00 PM | Verify the exact demo command at 2:15. Keep `.env` pointing at a database you have already reseeded once. |
| Concurrency test written against SQLite — passes, proves nothing | Point pytest at a real `money_test` Postgres database. Verify by making the test fail on purpose once. |
| Alembic migration works only on your machine | One teammate runs `alembic upgrade head` on a clean database before freeze. |
| Secret committed to git | `git log -p \| grep -i secret` at 2:15. `.env` in `.gitignore` from the first commit. |
| Money requests never get built | It is a named brief requirement. Protect the 12:45 block; cut frontend polish instead. |
| Demo runs long and judges cut you off before the race test | Rehearse with a timer. If you must compress, drop beats 5 and 9, never 4, 6 or 7. |
| Only one person can explain the code | Assign sections at 2:15. Every member fields at least one question in rehearsal. |
| Demo state is dirty from testing | `make demo` immediately before you present. Always. |

---

> [!IMPORTANT]
> **The one sentence to leave them with:**
> *"We built a small system that we can prove is correct, and we can show you exactly where
> it breaks and what we would do next."*
>
> That is the sentence a money-movement brief is actually asking for.

---

*PSTU IT Carnival 2026 · Money Movement Challenge · Final architecture, 29 August 2026*
