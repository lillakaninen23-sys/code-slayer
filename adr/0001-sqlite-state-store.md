# ADR 0001 — SQLite as the sole internal-state store

## Status
Accepted (Foundation Plan v0.1, Revision 2.1). Implemented in Phase 1.

## Context
Code Slayer needs a durable, transactional store for task state, audit
history, checkpoints, leases, and evidence that survives process crashes
between nearly any two operations, without requiring a server process to
run or be remembered — a hard requirement for a local-first tool.

## Decision
Use Python's stdlib `sqlite3` module against one SQLite database file per
`(repo_id, worktree_id)` pair, with:

- `PRAGMA foreign_keys = ON` — referential integrity enforced by the
  database, not just application discipline.
- `PRAGMA journal_mode = WAL` — a CLI invocation and a later daemon can
  share the file safely.
- `PRAGMA busy_timeout = 5000` — concurrent access waits briefly instead
  of failing immediately.
- `autocommit=True` (true autocommit, Python 3.12+) at the connection
  level, with every transaction opened and closed explicitly by
  `code_slayer.store.db.transaction()` — a single, auditable place in the
  codebase where "what is atomic with what" is decided, rather than
  relying on the sqlite3 module's historically ambiguous implicit
  transaction behavior around DDL.

No ORM. Raw, parameterized SQL in small repository classes (`TaskRepo`,
`ToolOperationsRepo`, `ContentStore`) is easier to audit line-by-line than
a generated query layer, and the schema is simple enough not to need one.

## Alternatives considered
- **Plain JSON/YAML files per task.** No atomic multi-row transactions —
  a crash mid-write can easily corrupt or partially update state. Rejected.
- **Postgres or another embedded/server database.** Violates local-first:
  it adds an operational dependency (a server to run, a port to bind) the
  Foundation Plan explicitly rules out. Rejected.
- **SQLAlchemy or another ORM.** Deferred, not rejected outright — the
  schema is small enough that raw SQL is currently easier to verify by
  inspection than a mapped abstraction would be.

## Consequences / known limitations
- `transaction()` is explicitly **not re-entrant** in Phase 1: nothing yet
  needs to compose two repositories' writes into one transaction, so
  nesting raises `RuntimeError` rather than doing something subtle. A
  later phase (e.g. an orchestrator that must commit a task transition and
  a checkpoint row together) may need a re-entrant or explicit
  save-point-based version of this helper — deferred, not designed around
  yet, since the composition it would support does not exist in Phase 1.
- SQLite gives ACID transactions over Code Slayer's *own* rows only. It
  says nothing about, and is never used to claim, atomicity for a
  filesystem write, a Git plumbing call, or a subprocess exec — see
  ADR 0006 (operation journal) for how that gap is covered.

## Tested by
`tests/unit/test_db.py` (creation, schema versioning, migration
idempotency and failure atomicity, foreign keys, reopen-after-shutdown,
transaction rollback) and `tests/integration/test_crash_recovery.py`
(real subprocess `os._exit()` mid-transaction).
