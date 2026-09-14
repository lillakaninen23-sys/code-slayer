# Durable leases, fencing, quiescence, and generic recovery (Phase 6)

Phase 6 establishes a durable answer to *which session currently has the
right to mutate a worktree*, and makes stale/recovered sessions unable to
mutate it again — enforced by durable SQLite state, never by process-local
memory, a PID, a hostname, or a timestamp alone. This revision (the Phase 6
completion patch) closes the takeover race the original implementation left
open: an `ACTIVE`-but-TTL-expired lease is never directly replaceable. It
must first pass through `QUIESCING`, where the previous owner's actual
liveness — not merely the clock — decides whether a new epoch may ever be
granted.

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
process-start timestamp, or a UUID alone. `worker_pid`/`worker_pid_started_at`
are recorded and, as of this patch, **are** consulted — but only to decide
whether a `QUIESCING` epoch may advance to `EXPIRED`, never as the fencing
decision itself. The durable fencing generation remains the sole authority
for "is this caller's claim currently valid."

`generation` is the durable **fencing token**: it starts at 1 on first
acquisition, strictly increases by exactly 1 on every later takeover,
never resets, and is never reused — the schema's pre-existing, Phase-1
`tool_operations.lease_generation` column carries a real value on every
Phase 4/5 mutation, populated at journal `STARTED` time. Entering or
leaving `QUIESCING` never burns a generation by itself — a new generation
is minted **only** at the `EXPIRED`/`RELEASED` → `ACTIVE` step, so a
takeover that turns out to be unsafe (owner still alive, liveness unknown)
costs nothing: the epoch is simply still generation N, still `QUIESCING`
or back to `ACTIVE` if reclaimed.

## The state machine

```
ACTIVE (generation N)
   │  TTL elapsed (heartbeat_at vs. now, wall-clock)
   ▼
QUIESCING (generation N, same owner identity)
   │  liveness evidence: is the recorded process actually gone?
   ├─ ALIVE   -> deny takeover; stays QUIESCING (or reclaimed by renew())
   ├─ UNKNOWN -> deny takeover (fail closed); stays QUIESCING
   └─ GONE, and no live recorded child either
        │
        ▼
      EXPIRED (generation N)
        │  a *subsequent* acquire() attempt
        ▼
      ACTIVE (generation N+1) — a genuinely new epoch
```

`RELEASED` is a separate terminal status reachable directly from `ACTIVE`
or `QUIESCING` via an authenticated `release()` call (the true owner
proving itself, not a takeover) — from there, a fresh `acquire()` grants
generation N+1 immediately, no quiescence needed, because there is no
"was it actually gone" question left to answer: the owner said so.

Every arrow above is its own committed `BEGIN IMMEDIATE` transaction —
never one multi-step transaction spanning `ACTIVE → EXPIRED → ACTIVE`, since
a crash mid-transaction would simply roll back to the pre-transaction state,
defeating the purpose of a durable intermediate step. `acquire()` internally
loops (bounded, `_MAX_ACQUIRE_STEPS = 4`) to walk as many safe steps as
possible in one call — typically all the way from `ACTIVE`(expired) to a
freshly granted `ACTIVE`(N+1) in a single call when the old owner is
provably gone — while remaining crash-safe: each step re-validates the
expected prior state under its own fresh transaction before writing, so a
crash or a concurrent racer between any two steps is simply re-observed
and re-decided from durable state, never assumed.

### `is_current()` vs. the reclaim path

Two different questions get two different checks, deliberately:

- **`is_current()`** — the gate every Phase 4/5 mutation path calls — is
  strictly `ACTIVE`-only. A lease entering `QUIESCING` fails this
  immediately, even before liveness is ever resolved: the instant there is
  *any* doubt about an epoch's authority, fencing must stop authorizing new
  mutations under it. This is what closes the original race: the previous
  implementation only checked `is_current()` and let a *new* epoch become
  `ACTIVE` immediately upon TTL expiry, so a stale mutator's fencing failed
  only once someone else had already raced ahead. Now, no new epoch can
  even be granted while the old one might still be alive, so there is at
  most one "live" epoch at any moment — the race is closed structurally,
  not by hoping the old caller notices first.
- **`_check_current()`** (used only by `renew()`/`release()`) accepts
  `ACTIVE` *or* `QUIESCING`. An authenticated call from the true owner is
  itself the strongest possible proof of liveness — stronger than any
  external `/proc`-based inference — so the real owner can always reclaim
  from quiescence review before eviction completes, simply by calling
  `renew()`.

## Process identity and liveness (`lease.liveness`)

A bare PID is never proof of identity: after a process exits, the OS is
free to reuse that same integer for something unrelated. `lease.liveness`
answers exactly one question, conservatively: *can we prove* the process
that last held a lease (or a still-tracked child subprocess) is gone? And
it makes a strict distinction, everywhere, between two outcomes that are
easy to conflate but must never be treated the same:

- **`GONE`** — positive, reliable evidence: `/proc/<pid>` itself is
  absent right now (`ENOENT`/`ESRCH`), or the pid exists but under a
  *different*, exactly-established identity than the one recorded (pid
  reuse — see below).
- **`UNKNOWN`** — liveness could not be safely established at all: no
  `/proc` on this platform, a permission or read failure, an incomplete
  or malformed `/proc/<pid>/stat`, a system boot time or clock-tick rate
  that could not be read or parsed, or no recorded identity to compare
  against. Failing to *prove* liveness is never treated as proof of
  death — this module never silently collapses an evidence-reading or
  -parsing failure into `GONE`.

`process_start_time(pid)` returns the *exact* `/proc`-derived process-start
identity for `pid`, or `None` if it could not be positively established.
This is deliberately **not** a formatted wall-clock timestamp: it encodes
the raw `(boot_time, starttime_ticks, clock_ticks_per_second)` triple
exactly as `/proc/stat`'s `btime` line and `/proc/<pid>/stat`'s `starttime`
field (field 22) report them, with no floating-point conversion or lossy
string round-trip involved. Every recorded pid — a worker's own
(`worker_pid`) and any subprocess it spawned and journaled
(`tool_operations.child_pid`) alike — is paired with this exact identity
at the moment it is recorded.

`check_process_liveness(pid, recorded_identity)` returns `Liveness.ALIVE`,
`GONE`, or `UNKNOWN`:

- pid does not exist right now (positive evidence) → `GONE`.
- pid exists, and its *current* exact identity matches the *recorded* one
  **exactly** → `ALIVE`, genuinely the same process. There is no time
  tolerance anywhere in this comparison — an earlier revision of this
  check accepted a start-time match within roughly two seconds to absorb
  `/proc`'s tick-granularity rounding, which was itself unsafe (two
  processes that started within that window would be indistinguishable);
  the fix compares the raw integers `/proc` reports directly, so
  rounding never enters the comparison and no tolerance is needed.
- pid exists, but its current exact identity does **not** match the
  recorded one, by any amount → the pid has been reused by a later,
  unrelated process; the *recorded* process is still `GONE`.
- anything that cannot be positively established either way (no `/proc`
  on this platform, permission denied, an unreadable/malformed
  `/proc/<pid>/stat`, an unreadable boot time or clock-tick rate, no
  recorded identity to compare against, or a recorded identity that does
  not even parse) → `UNKNOWN`, never guessed at either way — including
  when the pid is *definitely present* but its own identity could not be
  established (present is not the same as identified).
- Only Linux's `/proc` is used; anywhere else every query conservatively
  reports `UNKNOWN` rather than fabricate an answer. `LeaseManager` treats
  `UNKNOWN` exactly like `ALIVE` for the purpose of denying a takeover
  (fail closed) — the only value that ever permits `QUIESCING → EXPIRED`
  is a definite `GONE`.

`LeaseManager`'s `liveness_fn`/`child_liveness_fn` constructor parameters
default to this real, `/proc`-based check but are injectable — the same
pattern already used for `now_fn` — so the state machine's transition
logic can be exercised deterministically in `tests/unit/test_lease_manager.py`
without depending on real process timing, while
`tests/integration/test_lease_liveness.py` separately proves the real,
uninjected check against genuine OS processes (including a real subprocess
that has actually exited, and a simulated pid-reuse case built from a
currently-alive pid paired with a deliberately mismatched recorded start
time).

### Evidence must be semantically valid, not just syntactically parseable

A recorded identity that merely *parses* into three integers is not
automatically trusted: `boot_time` must be positive, `starttime_ticks`
must be non-negative, and `clock_ticks_per_second` must be positive —
Linux's own invariants for these fields, applied identically whether the
triple came from a live `/proc` read or was parsed back out of a
persisted `worker_pid_started_at`/`child_pid_started_at` column. A triple
violating any of them (e.g. `-1:-500:0`) is rejected exactly like a
parse failure — `UNKNOWN`, never a route to `GONE`.

One consequence worth naming explicitly: a lease persisted by a previous
implementation (which recorded either a bare pid or a formatted
timestamp, not this exact triple) can never be accepted as a valid exact
identity — it does not parse into three semantically valid integers.
Such a lease's `QUIESCING` review conservatively stays `UNKNOWN` and
never resolves to `EXPIRED` on its own; resolving it requires an
operator's explicit, out-of-band intervention (e.g. an authenticated
`release()`), not a schema migration or a guessed conversion of the old
value — either of which would undermine the exact-identity guarantee
this validation exists to provide.

### `/proc` disappearing between two observations

Checking that `/proc` exists and then opening `/proc/<pid>/stat` are two
separate observations with a gap between them. A `FileNotFoundError`
opening that file normally means the pid is gone — but if `/proc` itself
became unavailable in that gap, the failure proves nothing about the
specific pid, only that the evidence source itself vanished. Rather than
a retry loop, the failure handler re-confirms `/proc` is still present
*at that exact moment* before concluding `GONE`; if `/proc` has also
disappeared by then, the result is `UNKNOWN` instead. An ordinary,
definite "no such pid" while `/proc` is otherwise healthy is unaffected.

## Child subprocesses

A worker process being gone does not by itself prove a subprocess it
spawned is also gone (Foundation Plan §12's orphan-process concern) — a
`run_command` invocation records its child's pid and `/proc`-derived start
time (`tool_operations.child_pid`/`child_pid_started_at`, via
`ToolOperationsRepo.record_child_pid`, called from `on_spawn`) at the
moment the subprocess is launched. Before completing `QUIESCING → EXPIRED`
for a given epoch, `LeaseManager._has_live_child()` looks up every
unresolved (`STARTED`/`UNKNOWN`) operation under that exact `worktree_id` +
`lease_generation` with a recorded `child_pid`, and checks each with the
same reuse-safe `check_process_liveness` used for the worker itself.

This check is deliberately conservative: **both** `ALIVE` *and* `UNKNOWN`
block the transition to `EXPIRED` — only a definite `GONE` permits it.
Guessing "probably gone" from an unparseable or unrecorded start time would
risk letting a takeover proceed alongside a subprocess that is, in fact,
still running; every capability's own bounded timeout keeps this window
small in practice, and no process is ever killed to resolve it — Phase 6
prefers conservative, indefinite quiescence over any process-killing
behavior.

## Fencing integration: Phase 4 and Phase 5

`ToolExecutor` and `CheckpointManager` require a `LeaseHandle` (optional
for `CheckpointManager`, since `reconcile()` never consults it) and check
`LeaseManager.is_current()` at multiple points:

1. **Before policy** (task state → **lease validity** → policy →
   journaling → effect → evidence → finalization): a stale caller is
   refused before its request is even evaluated, with its own audit event
   (`FENCE_STALE_REJECTED`) distinct from an ordinary `POLICY_DENIED`.
2. **Immediately before the actual effect** — for file mutations, the
   final `is_current()` recheck and the filesystem mutation both happen
   inside the *same* `BEGIN IMMEDIATE` writer transaction. Because SQLite
   holds an exclusive write lock for that transaction's whole duration, no
   competing writer — including a `LeaseManager.release()`/`acquire()`
   attempting a real takeover on a wholly separate connection — can even
   *start* its own transaction until this one commits or rolls back. This
   is proven, not just argued, in
   `tests/integration/test_lease_fencing.py::
   test_file_mutation_transaction_serializes_against_a_concurrent_takeover`,
   which opens a genuinely second SQLite connection to the same database
   file and shows its takeover attempt fails with SQLite's own "database is
   locked" while the first transaction is open, then succeeds normally once
   it has committed.
3. **Immediately before `run_command`'s finalization** — the same
   recheck-then-finalize-in-one-transaction pattern. `run_command`'s
   capability profile remains restricted/read-only in this phase (no
   mutating command capability exists to broaden); what matters is that a
   stale session cannot durably record a command's result as authoritative
   once superseded, proven by a real takeover injected between the
   subprocess completing and finalization in
   `test_takeover_during_command_run_denies_finalization`.

`CheckpointManager.create()` applies the same lease-then-policy ordering
for *starting* a new checkpoint attempt, plus one more, deeper check this
patch adds:

- **Deepest-boundary revalidation before the ref write.** The original
  design validated fencing only before `build_tree()` began, then trusted
  the checkpoint's own Git-ref evidence for everything after — including
  `create_ref`, the one call that makes a checkpoint externally visible.
  Building the tree and commit objects is otherwise inert (they are just
  unreferenced Git objects until named), so `create()` now re-checks
  `is_current()` one more time immediately before `create_ref`, the last
  possible moment to refuse making a checkpoint visible under authority
  that may have been superseded while the (Git-plumbing-heavy, potentially
  slow) tree/commit construction ran. If a takeover completed in that
  window, the attempt fails (`stale_fencing_token`) and the built commit
  simply stays an unreferenced, harmless object — the new, current lease
  holder can immediately start a fresh attempt. Proven in
  `test_checkpoint_takeover_before_ref_write_prevents_it_becoming_visible`.
  Once the ref *does* exist under a still-valid epoch, that evidence
  remains authoritative regardless of what happens to lease state
  afterward — `_finalize()` is not re-gated on the lease, deliberately: a
  checkpoint's truth is Git-ref evidence, not lease state, and with
  `QUIESCING` now preventing a new epoch from ever being granted while the
  old one might still be running, at most one epoch is ever live at a
  time, closing the original race without needing to touch `_finalize()`'s
  own evidence-based semantics. Proven in
  `test_checkpoint_reconcile_still_evidence_based_once_ref_already_exists`.
  Phase 5's recovery semantics (`reconcile()`, the CAS ref write, the
  duplicate-`seq` guard) are otherwise untouched.

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
recovery path as a side effect of merely calling `create()`. The
standalone `reconcile()` method is deliberately exempt from this gate — it
is the explicit owner/operator escape hatch this phase's brief calls for,
always available regardless of lease state, and it never auto-resolves
anything beyond what its own Git-ref evidence proves.

## Generic recovery

`lease.recovery` adds the framework the brief asks for, without pretending
to solve reconciliation for every tool, and — as of this patch — with full
`QUIESCING` awareness:

- `discover_unresolved()` lists every `STARTED`/`UNKNOWN` `tool_operations`
  row (optionally scoped to one task), each annotated with the epoch it
  started under and the worktree's *current* lease row (generation and
  status) — purely informational; discovery never resolves anything.
- `UnresolvedOperation.epoch_state` classifies each operation's epoch
  against the current lease into exactly one of: `"no_lease"` (the
  worktree currently has no lease row at all), `"current_active"` (the
  operation's epoch *is* the current lease, and it is `ACTIVE`),
  `"current_quiescing"` (the operation's epoch *is* the current lease,
  and its owner's liveness is right now unresolved — this *is* the
  "unknown process liveness" case the brief asks recovery to be able to
  name), `"current_expired"` (the epoch's owner has been proven gone but
  no successor has acquired yet), `"current_released"` (an explicit
  release raced an unfinished operation), `"stale"` (a strictly later
  generation has already become fully `ACTIVE` — this epoch is
  conclusively superseded), or `"unknown"` (the operation's own
  `lease_generation` was never recorded, e.g. journaled before Phase 6
  populated it — not comparable either way). `stale_epoch` (the original,
  coarser boolean) is kept for backward compatibility.
- Critically, none of this classification ever resolves an operation's
  outcome by itself — `"current_quiescing"`/`"current_expired"`/`"stale"`
  are reported, never acted on directly. `reconcile_supported()` still
  dispatches only rows whose `tool_name` already has a real reconciler —
  today, exactly `checkpoint_create`, reusing `CheckpointManager.reconcile()`'s
  existing, unmodified, Git-ref-evidence semantics — and leaves every
  other tool exactly as found. Tool-specific evidence, never a lease or
  liveness classification alone, decides an operation's outcome.

Process death alone is never treated as proof of anything beyond what it
proves for the lease state machine itself: this module never infers a
`STARTED` row's true outcome from the fact that a session looks stale,
quiescing, or expired.

## Known limitations

- **No lease history.** `worker_leases` holds only the *current* epoch
  per worktree; past epochs are reconstructable only via the audit log
  (`LEASE_ACQUIRED`/`LEASE_RENEWED`/`LEASE_RELEASED`/`LEASE_QUIESCING`/
  `LEASE_EXPIRED`/`FENCE_STALE_REJECTED`), not a dedicated table.
- **No lease TTL policy engine.** `LeaseManager(ttl_seconds=...)` is a
  single, fixed number per manager instance (`DEFAULT_LEASE_TTL_SECONDS =
  300.0`), not a configurable-per-task or adaptive policy.
- **Liveness is Linux-`/proc`-only.** On any platform without `/proc`,
  every liveness check reports `UNKNOWN`, which means a `QUIESCING` lease
  can never durably resolve to `EXPIRED` there — quiescence would persist
  until an operator intervenes (e.g. by releasing explicitly with proof of
  the old session being gone by some other means). This is the fail-closed
  behavior working as intended, not a bug, but it is a real availability
  cost on such a platform.
- **Child liveness is best-effort even on Linux.** A child pid whose
  `/proc` start time could not be captured at spawn time (e.g. it had
  already exited by the time `on_spawn` ran) is recorded with a `NULL`
  start time, which `check_process_liveness` reports as `UNKNOWN` for as
  long as that pid still exists — conservatively blocking quiescence
  rather than guessing, but potentially for longer than strictly
  necessary.
- **No process killing.** Nothing in this phase ever terminates a
  process, worker or child, to force quiescence to resolve faster; a
  `QUIESCING` epoch whose owner (or a recorded child) is genuinely alive
  simply stays `QUIESCING` (or is reclaimed by its true owner via
  `renew()`) until liveness evidence changes.
- **A lease recorded under a pre-exact-identity format stays
  `QUIESCING` indefinitely.** This is the direct consequence of never
  guess-converting old evidence (see above) — an operator must resolve
  such a lease explicitly rather than wait for automatic quiescence
  resolution, which will never happen for it.
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

`tests/unit/test_lease_manager.py` (acquire/renew/release/takeover, the
full `QUIESCING` cascade against injected fake liveness for live-owner/
dead-owner/unknown-liveness/live-child cases, expiry, malformed-data, a
real two-connection concurrent-acquire test, and a restart-while-quiescing
test that reopens the database mid-cascade), `tests/unit/test_generic_recovery.py`
(discovery, `epoch_state` classification including `current_quiescing`,
dispatch-only-where-supported), `tests/integration/test_lease_crash.py`
(real subprocess crashes mid-acquire/renew/release/takeover, each proven
to roll back atomically via a reopened connection),
`tests/integration/test_lease_liveness.py` (the real, uninjected `/proc`
liveness check against genuine OS processes: a real subprocess that has
exited, a simulated pid-reuse case, a real cross-process takeover of a
lease whose owner was a separate, now-exited process, the mirror
still-alive-owner denial, and restart-while-quiescing at the integration
level), and `tests/integration/test_lease_fencing.py` (every Phase 4
capability plus checkpoint creation refusing a stale lease, a current
lease still working normally, takeover races injected at the exact
boundaries that matter, the deepest-boundary checkpoint-ref race, and the
two-connection proof that a file mutation's writer transaction serializes
against a concurrent takeover attempt). All pre-existing Phase 1–5 tests
continue to pass, updated only to acquire and pass a real lease where
Phase 4/5 entry points now require one (`tests/repo_helpers.acquire_lease`).
