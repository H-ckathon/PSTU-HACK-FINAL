# AI-Assisted Development

The rules allowed AI-assisted development and required us to understand, explain
and defend what we built. This document is the honest record of how we used it,
what we decided ourselves, and — most usefully — the places where AI-generated
code was **wrong** and how we found out.

---

## 1. How we worked

We used AI the way you would use a fast, well-read pair programmer who has never
seen your requirements: it drafts, we decide.

The loop, seven times over:

1. **We** fixed the architecture and the guarantees first, before any code.
2. **We** specified one block: what it must do, what must be impossible.
3. **AI** drafted the block.
4. **We** ran it against a real PostgreSQL database and read the failures.
5. **We** fixed what the tests exposed, and wrote the reasoning into the code.

Step 4 is where the value was. Three times, code that looked correct — and that
we could have shipped without noticing — failed against a real database. Those
three are documented in section 4, and they are the strongest evidence we
understand this system rather than merely possessing it.

---

## 2. What we decided, not the AI

These are the decisions that shaped the project. Each was ours; the AI wrote
code *after* the decision, not instead of it.

| Decision | Why we made it |
|---|---|
| **A ledger, not a balance column** | `UPDATE balance = balance - 500` cannot be audited and is where lost updates live. Signed append-only entries let us *prove* correctness with one query instead of asserting it in prose. |
| **`SUM(entries) = 0` as the headline invariant** | We wanted one sentence a judge could verify in five seconds. Signing the amounts is what makes that possible. |
| **A modular monolith** | Splitting the ledger into a service turns one ACID transaction into a distributed saga. More code, less safety, in six hours. |
| **Idempotency keys** | Not in the brief, and absent from most reference implementations of this problem. It is the single most visible correctness feature in a live demo. |
| **A separate transaction PIN** | A stolen session should not be enough to move money. Two independent secrets, cheap to build. |
| **`CHECK (balance >= 0)` in the database** | Defence in depth. If our service layer is wrong, PostgreSQL still refuses. |
| **Append-only enforced by trigger** | A `REVOKE` needs role-switching to demonstrate. A trigger throws in front of the judges with no setup. |
| **Rate limiting keyed by account, not IP** | A campus or office shares one NAT address. IP-only limits would throttle bystanders. |
| **No cache, no queue, no Docker, no microservices** | Each was considered and rejected with a stated trigger for when it would become right. Restraint that is named reads as judgement; restraint that is silent reads as oversight. |
| **No CDN in the frontend** | The page must render with the venue wifi down. |

We also rejected AI suggestions. Three worth naming:

- **Tailwind via CDN for the frontend.** Rejected: a network dependency in the one artefact the judges look at. We wrote the CSS by hand and the page now works offline.
- **Storing the balance as an integer count of poisha.** Reasonable, and used by many systems — we chose `NUMERIC(15,2)` because it removes unit-conversion bugs at the API boundary and is readable in `psql` during a demo.
- **A background job to expire money requests.** Rejected: there is no scheduler in this system, so an unswept request would display as pending while being unpayable. We compute expiry on read instead.

---

## 3. Prompts that actually worked

The pattern that produced good code was **specifying the guarantee, not the
implementation** — and always naming what must be impossible.

**Weak prompt** (produced code we threw away):

> "Write a transfer endpoint for a money app."

**Strong prompt** (produced most of `transfer_service.py`):

> "Write a transfer function in SQLAlchemy 2.x sync. Requirements, in order:
> a retried request with the same idempotency key must return the original
> transaction rather than moving money twice; the PIN must be verified before
> anything is locked; both wallets must be locked `FOR UPDATE` in ascending id
> order so A→B and B→A cannot deadlock; the balance must be read *after* the
> lock is held; the transaction row, both ledger entries, both balance updates
> and the audit row must commit atomically or not at all. Explain in comments
> which line prevents which failure."

Three more that earned their keep:

> "Write a test that fires 20 concurrent transfers against a wallet that can
> only fund 10, using a separate Session per thread, and assert that exactly 10
> succeed and the ledger still sums to zero."

That is the test that caught the biggest bug. See 4.1.

> "This migration must be readable as a design document. Write it as explicit
> SQL rather than Alembic operations, and put every guarantee in a constraint
> rather than in application code."

> "What is wrong with this reconciliation endpoint, given that it always
> returns green in our tests?"

That question led us to write `test_reconcile_detects_a_planted_inconsistency`,
which tampers with a wallet balance directly and asserts the check *catches* it.
Without that test, a green light proved nothing.

---

## 4. Where the AI was wrong

The three bugs below were all in AI-drafted code, all looked correct on review,
and all were caught by running against a real database. This section is the part
we would most want a judge to read.

### 4.1 The lock was real; the balance was stale

`ledger_service.lock_wallets` issued `SELECT ... FOR UPDATE` in sorted id order.
The SQL was correct. PostgreSQL took the row lock correctly. But `user.wallet`
is eagerly joined, so the `Wallet` object was already in SQLAlchemy's identity
map — and **SQLAlchemy returned the cached Python object instead of the row it
had just locked and read.**

That is precisely the lost-update bug the locking exists to prevent, hiding one
layer above the SQL.

```
AssertionError: expected exactly 10 successes, got 20
```

Twenty transfers of ৳10,000 succeeded against a wallet holding ৳100,000, and
even the `CHECK (balance >= 0)` constraint never fired — because every
transaction computed `100000 − 10000` from its own stale copy and wrote `90000`.
Last writer wins.

The fix is one execution option:

```python
.execution_options(populate_existing=True)
```

**Reading the code would never have found this.** Twenty real threads against a
real database did. It is also the reason `tests/conftest.py` refuses to run
against anything but PostgreSQL: on SQLite this test passes while proving
nothing.

### 4.2 An audit write could fail a money operation

`audit_log.ip_address` is a PostgreSQL `INET` column — good, because it gives
free validation and real network-range queries later. But `INET` rejects
anything that is not an address, and a proxy or test client can present a
non-IP host. Registration crashed with:

```
psycopg2.errors.InvalidTextRepresentation: invalid input syntax for type inet: "testclient"
```

The wrong fix is to weaken the column to `VARCHAR`. We validate at the edge
instead — `client_ip()` returns `None` for anything unparseable — so the column
keeps its strong type and logging can never be the thing that breaks a transfer.

### 4.3 Reuse detection revoked one link, not the chain

Refresh rotation created a new session each time, and detecting a replayed token
blocked only *that* session. Its descendants stayed alive — so an attacker who
stole a token, used it once, and let the victim's replay trip the alarm would
keep working access. That is exactly the failure mode reuse detection exists to
prevent.

Migration `0002` adds `sessions.family_id`; one detection now revokes the whole
family in a single statement.

### And one integration failure worth mentioning

Adding `@limiter.limit` broke 42 of 44 tests at once, with every write endpoint
returning `422 {"loc": ["query", "body"]}`. Cause: `from __future__ import
annotations` turns annotations into strings, and slowapi's wrapper reports its
own module globals — so FastAPI could not resolve `TransferRequest` from
`slowapi.extension`'s namespace and silently demoted the request body to a query
parameter. Removing that import from the three limited routers fixed all 42.
There is a comment at the top of each explaining why it must stay out.

---

## 5. What we can defend, line by line

Any member of the team can walk through:

- `app/services/transfer_service.py` — the five guarantees and which line enforces each.
- `migrations/versions/0001_initial_schema.py` — every constraint and why it lives in the database rather than in Python.
- `app/services/ledger_service.py` — why `post_double_entry` never commits, and why `populate_existing` is not optional.
- `app/services/request_service.py` — the three independent defences against paying a request twice.
- `tests/test_concurrency.py` — what each of the three tests would fail to prove on SQLite.

---

## 6. Honest summary

AI wrote most of the characters in this repository. It did not choose the ledger
model, the invariants, the lock ordering, the idempotency mechanism, the
deliberate omissions, or the tests that caught its own mistakes. Three times it
produced code that was wrong in ways that would have survived code review and
failed in production — and the reason we found all three is that we insisted on
running against a real database with real concurrency before believing anything.

That insistence is the engineering decision we would most like to be judged on.
