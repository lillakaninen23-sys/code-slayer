# ADR 0005 — A durable, write-ahead tool-operation journal

## Status
Accepted (Foundation Plan v0.1, Revision 2.1, INV-13/INV-14). Storage and
repository API implemented in Phase 1; no real mutating tool runs through
it yet.

## Context
SQLite can make Code Slayer's own rows atomic with each other. It cannot
make a filesystem write, a Git plumbing call, or a subprocess exec atomic
with a database row — those are separate operations in the real world.
Revision 1 of the Foundation Plan under-stated this; Revision 2 made the
gap explicit:

```
DB row: tool_operations.status = STARTED
  → the actual filesystem/Git/subprocess side effect happens
  → the process dies before the row can be updated
  → DB never receives SUCCEEDED or FAILED
```

## Decision
`tool_operations` is a write-ahead journal: a row is inserted with
`status = STARTED` **before** any external side effect runs, durably
committed via `code_slayer.store.db.transaction()`. Once the side effect
resolves, the row is updated to `SUCCEEDED`/`FAILED` (or left/flipped to
`UNKNOWN` if resolution is itself interrupted). A future resume procedure
reconciles any row still `STARTED`/`UNKNOWN` against real repo/filesystem
state before retrying whatever it describes (Foundation Plan §15) — not
implemented in Phase 1, since no tool exists yet to journal.

The row's shape already includes fields no Phase 1 code populates with a
real value, so a later phase does not need a schema migration to add
them:

- `lease_generation` — **nullable in Phase 1**, a deliberate, explicitly
  authorized deviation from the canonical Revision 2.1 schema (which
  specifies `NOT NULL`), because no lease manager exists yet to assign a
  real fencing generation. A future migration makes this column `NOT
  NULL` once the lease manager exists.
- `child_pid` / `child_pid_started_at` — for a future `QUIESCING`
  reconciliation (Foundation Plan §12) to find and check a subprocess
  that outlived the lease that authorized it.

`request_hash` is computed by
`tool_operations_repo.compute_request_hash(tool_name, params)` —
`sha256(canonical_json({"tool": ..., "params": ...}))` — so the same
logical call always hashes identically regardless of parameter ordering.

## Alternatives considered
- **Recording the outcome only after the side effect completes.** Rejected:
  this is exactly what leaves the crash window in the diagram above open.
- **Trying to make the side effect itself transactional with SQLite** (e.g.
  via a two-phase-commit-like protocol). Rejected as unnecessary complexity
  for a local, single-machine tool — the journal-plus-reconciliation
  pattern gives crash safety without needing the side effect to
  understand transactions at all.

## Consequences / known limitations
- Reconciliation logic itself (comparing `before_evidence`/`after_evidence`
  against real state to classify an `UNKNOWN` row) is not implemented in
  Phase 1 — there is no tool layer yet to produce meaningful evidence
  hashes. This ADR covers the storage and API only.
- No foreign key from `tool_operations.worktree_id` to
  `worker_leases.worktree_id`: the lease manager does not populate
  `worker_leases` yet, and a real FK there would make it impossible to
  journal an operation independently of lease-manager existence — which
  is exactly what Phase 1 needs to be testable on its own.

## Tested by
`tests/unit/test_tool_operations.py`: STARTED persists across a simulated
reopen; STARTED→SUCCEEDED and STARTED→FAILED; an unresolved STARTED can be
flipped to UNKNOWN and later reconciled; operation IDs are unique;
request hashes are deterministic regardless of parameter order;
`lease_generation` is confirmed nullable in this phase.
