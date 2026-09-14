# Durable leases, fencing, and generic recovery (Phase 6)

Phase 6 establishes a durable answer to *which session currently has the
right to mutate a worktree*, and makes stale/recovered sessions unable to
mutate it again — enforced by durable SQLite state, never by process-local
memory, a PID, a hostname, or a timestamp alone.

## Lease model

`worker_leases.worktree_id` is the primary key: one worktree, one current
lease, one current **ownership epoch** — matching Foundation invariant
INV-2 (at most one non-terminal task per worktree) and the comment
`tasks.worktree_id` has carried since Phase 1: "the unit of mutation
ownership (future lease manager)". A lease is therefore worktree-scoped,
not task-scoped, though it records which task it was acquired for.

An ownership epoch is `(worker_id, worker_session_id, generation)`. A
caller proves current authority with a `LeaseHandle` whose full epoch
still matches the persisted row exactly — never with a PID, a hostname, a
process-start timestamp, or a UUID alone; those are recorded (`worker_pid`,
`worker_pid_started_at`) as forensic metadata only, never as the fencing
decision itself (deliberately not implemented: real process-liveness
probing — see Known limitations).

`generation` is the durable **fencing token**: it starts at 1 on first
acquisition, strictly increases by exactly 1 on every later takeover,
never resets, and is never reused — the schema's pre-existing, Phase-1
`tool_operations.lease_generation` column now carries a real value on
every Phase 4/5 mutation, populated at journal `STARTED` time.

## Expiry vs. fencing — two different questions

*Expiry* (`heartbeat_at` vs. a TTL, wall-clock) decides whether **takeover
may occur**. *Fencing* (an exact epoch-tuple match) decides whether **a
specific caller's claimed authority is still valid**. These are
deliberately independent:

- A clock **jump forward** can make a still-legitimately-active lease look
  expired to an outside evaluator, permitting a takeover that displaces a
  session that was not actually gone. This is an availability problem, not
  a safety one: the displaced session's fencing token is immediately and
  permanently stale everywhere it matters (Phase 4/5 mutation paths,
  renew, release) the moment a takeover actually happens — it can never
  regain authority by force, only by acquiring fresh.
- A clock **rollback** can only make an active lease look *less* expired,
  which never grants anyone new authority.
- Fencing comparisons themselves (`is_current`, `_check_current`) never
  read the clock at all — only `acquire`'s takeover branch and
  `expire_if_stale` do.

The current holder may always `renew()` as long as its own epoch still
matches the persisted row — renewing *is* proof of liveness, independent
of how much time has passed since the last heartbeat, right up until a
takeover has actually replaced the row. No background daemon exists here;
acquire/renew/release/`expire_if_stale` are explicit primitives a caller
invokes, never scheduled by this module.

## Acquire / renew / release / takeover

`lease.manager.LeaseManager`, backed by `store.lease_repo.LeaseRepo`
(schema v1's pre-existing, previously schema-only `worker_leases` table —
**no migration**; `tool_operations.lease_generation` also needed none,
since it was already nullable and Phase 6 only starts populating it):

- **acquire**: fresh (no row yet) always succeeds at generation 1. Denies
  `lease_held_and_not_expired` if an ACTIVE, unexpired row belongs to a
  *different* epoch; denies `already_held_by_caller_use_renew` if it's the
  caller's own (acquire is not idempotent renewal — that's `renew()`'s
  job, kept as a distinct, explicit operation). Otherwise (no row,
  `RELEASED`, `EXPIRED`, or ACTIVE-but-past-TTL) takes over: generation
  strictly increments, `ACTIVE`. One `BEGIN IMMEDIATE` transaction per
  attempt — SQLite's own write lock serializes concurrent acquirers into
  exactly one winner (proven with two real, separate connections in
  `tests/unit/test_lease_manager.py::test_concurrent_acquire_exactly_one_winner`).
- **renew**: succeeds only if the caller's exact epoch still matches and
  status is `ACTIVE`; refreshes `heartbeat_at`. A stale token is denied
  `stale_fencing_token`.
- **release**: succeeds only for the exact current epoch; sets `RELEASED`.
  Never touches task state, checkpoints, or unresolved operations — those
  are separate concerns entirely (§9 of the brief this phase answers to).
  A stale or wrong-identity release is denied, never silently a no-op,
  and can never clear a newer owner's lease.
- **`expire_if_stale`**: durably marks an ACTIVE-but-past-TTL lease
  `EXPIRED` *without* acquiring it — makes staleness durably visible (to
  generic recovery, an operator, or a monitor) even before anyone takes
  over.
- **`is_current`**: the read-only check every Phase 4/5 mutation path
  calls — safe from inside a caller's own already-open write transaction
  (a plain `SELECT`, never its own transaction).

Malformed persisted lease data (a non-positive/non-integer generation,
an unparseable timestamp, an empty identity, an unrecognized status) is
never trusted for a decision — every entry point denies closed on it
(`malformed_persisted_lease`), never guesses a fallback.

## Fencing integration: Phase 4 and Phase 5

`ToolExecutor` and `CheckpointManager` now require a `LeaseHandle` (an
optional one for `CheckpointManager`, since `reconcile()` never consults
it — see below) and check `LeaseManager.is_current()` at two points for
every Phase 4 capability:

1. **Before policy** (task state → **lease validity** → policy →
   journaling → effect → evidence → finalization): a stale caller is
   refused before its request is even evaluated, with its own audit event
   (`FENCE_STALE_REJECTED`) distinct from an ordinary `POLICY_DENIED`. A
   valid lease is *not* permission — policy still decides everything it
   already decided; an invalid lease is an unconditional refusal policy
   never gets to override.
2. **Immediately before the actual effect** — the second recheck
   transaction Phase 4 already had (`preconditions_changed`) for file
   mutations, and immediately before `_finish()` for `run_command` — this
   is the check that actually matters for a long-running operation: task
   workflow state (`IMPLEMENTING`) does not change merely because a lease
   was taken over (§17 of the brief — lease state is orthogonal to
   workflow state), so nothing about the existing baseline/ownership
   recheck would otherwise ever catch a takeover racing an in-flight
   mutation. Proven with a real takeover injected between the two
   checkpoints in `tests/integration/test_lease_fencing.py::
   test_takeover_between_initial_check_and_mutation_denies_write`.

`CheckpointManager.create()` applies the same two-part ordering for
*starting* a new checkpoint attempt. It does **not** re-check fencing
inside `_finalize()`, deliberately: checkpoint truth is Git-ref evidence,
not lease state. Once a commit exists, recording it and transitioning
`READY_FOR_CHECKPOINT -> CHECKPOINTED` records an objective fact that
happened under authority validly held at the time the attempt started;
the state machine's own expected-state check (Phase 2) already refuses a
finalize that would conflict with whatever a newer session has since
legitimately done to the task, with no extra fencing logic needed —
proven in `test_checkpoint_takeover_during_git_work_still_finalizes_from_evidence`.
A stale lease still fully blocks *starting a new* checkpoint attempt.

An additional, structural protection worth naming explicitly: while a
checkpoint attempt's journal entry is `STARTED` (its whole git-plumbing
window), Phase 4's own pre-existing "unresolved operation" check denies
*every* file mutation for that worktree, for *any* caller, lease or no
lease — so a newly-arrived session cannot mutate task-owned content out
from under an in-flight checkpoint attempt regardless of who currently
holds the lease.

## `create()`'s automatic reconciliation is itself lease-gated

`create()` first attempts to resolve any pending `checkpoint_create`
operation automatically — but only when the *caller* currently holds a
valid lease. An unauthenticated or stale caller cannot trigger this
recovery path as a side effect of merely calling `create()`
(`tests/integration/test_lease_fencing.py::
test_stale_caller_cannot_trigger_automatic_reconciliation`). The
standalone `reconcile()` method is deliberately exempt from this gate —
it is the explicit owner/operator escape hatch this phase's §18 calls
for ("if owner intervention is required to resolve an uncertain
operation, represent it explicitly"), always available regardless of
lease state, and it never auto-resolves anything beyond what its own
Git-ref evidence proves.

## Generic recovery

`lease.recovery` adds the framework §13 asks for, without pretending to
solve reconciliation for every tool:

- `discover_unresolved()` lists every `STARTED`/`UNKNOWN` `tool_operations`
  row (optionally scoped to one task), each annotated with the epoch it
  started under and the worktree's *current* epoch — purely
  informational (`stale_epoch`); discovery never resolves anything.
- `reconcile_supported()` discovers, then dispatches only rows whose
  `tool_name` already has a real reconciler — today, exactly
  `checkpoint_create`, reusing `CheckpointManager.reconcile()`'s existing,
  unmodified, Git-ref-evidence semantics. Every other tool (every Phase 4
  file/command capability, which has no reconciler at all) is reported
  and left exactly as found — never guessed at, never auto-resolved,
  matching Phase 4's own accepted limitation that a stuck file mutation
  blocks the worktree until a future phase gives it a reconciler.

Process death alone is never treated as proof of anything: this module
never infers a `STARTED` row's true outcome from the fact that a session
looks stale.

## Known limitations

- **No process-liveness probing.** `worker_pid`/`worker_pid_started_at`
  are recorded but never consulted for a decision — Phase 6 does not
  implement checking whether a recorded PID (with its start time, to
  guard against PID reuse) is actually still alive. Consequently, if
  session B takes over after session A's lease is durably invalidated
  (expired/released) while A's *process* happens to still be physically
  running some leftover work, and B then calls `create()` (which requires
  B's own lease to be current, so this can only happen after a genuine,
  legitimate takeover), B's lease-gated automatic reconciliation could
  observe A's checkpoint ref as not-yet-created and mark A's operation
  `FAILED` before A's still-running attempt actually finishes. This does
  not corrupt anything — `create_ref`'s compare-and-swap and
  `CheckpointRepo`'s duplicate-`seq` guard together ensure at most one of
  A's or B's commits is ever durably recorded as *the* checkpoint for that
  `seq`, atomically, and the loser's `create()` call safely reports
  `UNKNOWN`/`FAILED` rather than corrupting state — but A's specific
  attempt can be wasted. Closing this fully needs subprocess/session
  liveness tracking (the `QUIESCING` status and Foundation Plan §12's
  orphan-process reconciliation), explicitly deferred.
- **`QUIESCING` is schema-valid but unused.** Nothing in Phase 6 ever
  writes it; a takeover treats it the same as `RELEASED`/`EXPIRED` (both
  are simply "not currently `ACTIVE`, so eligible").
- **No lease history.** `worker_leases` holds only the *current* epoch
  per worktree; past epochs are reconstructable only via the audit log
  (`LEASE_ACQUIRED`/`LEASE_RENEWED`/`LEASE_RELEASED`/`LEASE_EXPIRED`/
  `FENCE_STALE_REJECTED`), not a dedicated table.
- **No lease TTL policy engine.** `LeaseManager(ttl_seconds=...)` is a
  single, fixed number per manager instance (`DEFAULT_LEASE_TTL_SECONDS =
  300.0`), not a configurable-per-task or adaptive policy.
- Generic recovery dispatches to exactly one reconciler
  (`checkpoint_create`); Phase 4's file/command capabilities remain
  without one, exactly as documented in `docs/TOOLS_AND_POLICY.md`.

## Explicitly deferred (Phase 7+)

No model, provider, or worker runtime; no scheduler or orchestrator; no
autonomous agent loop or daemon; no WebUI; no environment/VM manager; no
knowledge or training system; no network access of any kind. A lease/
session is infrastructure for *whichever* future worker exists — it does
not itself become one.

## Tests

`tests/unit/test_lease_manager.py` (acquire/renew/release/takeover/expiry/
malformed-data, plus a real two-connection concurrent-acquire test),
`tests/unit/test_generic_recovery.py` (discovery, staleness annotation,
dispatch-only-where-supported), `tests/integration/test_lease_crash.py`
(real subprocess crashes mid-acquire/renew/release/takeover, each proven
to roll back atomically via a reopened connection), and
`tests/integration/test_lease_fencing.py` (every Phase 4 capability plus
checkpoint creation refusing a stale lease, a current lease still working
normally, and takeover races injected at the exact boundaries that
matter). All pre-existing Phase 1–5 tests continue to pass, updated only
to acquire and pass a real lease where Phase 4/5 entry points now require
one (`tests/repo_helpers.acquire_lease`).
