# Durable task state machine (Phase 2)

`code_slayer.core.TaskStateMachine` is the production authority for task
state changes. `TaskRepo.create()` still creates the initial `CREATED` task.
`TaskRepo.record_transition()` is a persistence primitive, retained for
foundation tests; production code must not bypass the state machine through
it, its private transaction-scoped helper, or direct SQL.

## Exact transition model

The ordinary path is:

```text
CREATED → INSPECTING → BASELINED → PLANNING → PLANNED
→ IMPLEMENTING → VERIFYING → REVIEWING → READY_FOR_CHECKPOINT
→ CHECKPOINTED → COMPLETED
```

Additional ordinary edges are:

- `CHECKPOINTED → IMPLEMENTING`
- `VERIFYING → REPAIRING → VERIFYING`
- `REVIEWING → REPAIRING`

All eleven ordinary, non-terminal states (including `CREATED`, the
milestones, and `REPAIRING`) are active and interruptible. Each can enter
`INTERRUPTED_RESUMABLE`, `BLOCKED`, or `FAILED`.

`INTERRUPTED_RESUMABLE` and `BLOCKED` can return only to their own durable
active origin, or enter `FAILED`. There is no edge between these two
suspended states. Neither is terminal or active; both still occupy the
foundation's single non-terminal task slot for their worktree.

`COMPLETED` and `FAILED` have no outgoing edges, including self-edges.
Phase 7.7a adds one conditional edge: `IMPLEMENTING → COMPLETED` for tasks
explicitly created with `config.execution_kind="bounded_read_only_turn"`.
It requires `completion_decision=True`; the state machine also checks the
journal in the same write transaction and refuses unresolved or non-read-only
operations. A bounded read-only turn has no repository changes to checkpoint.
Ordinary implementation jobs retain the checkpoint completion requirement.

There are no other edges. The immutable static table lives in
`core/transitions.py`; origin-dependent returns are validated there too.
Shared classification lives in `core/states.py`.

## Phase and origin in schema v1

Schema v1 already has `tasks.state` and nullable `tasks.current_phase`.
Phase 2 gives the latter this explicit contract:

| State | `current_phase` |
| --- | --- |
| `CREATED` | `NULL` (no phase entered yet, as in Phase 1) |
| Other active state | That state's canonical uppercase string |
| `INTERRUPTED_RESUMABLE` or `BLOCKED` | The exact active origin's canonical string |
| Terminal state | The preceding phase, retained for diagnosis |

The suspension state records *why execution is paused*; the phase column
retains *where it paused*. For example, interruption of `REVIEWING` persists
`(INTERRUPTED_RESUMABLE, REVIEWING)`, and blocking `REPAIRING` persists
`(BLOCKED, REPAIRING)`. Suspending `CREATED` stores the origin `CREATED`;
returning to it restores its original `NULL` phase.

Resume names the original state as `to_state`. Blocked resolution also
requires `reconciled_target` to name that same state explicitly. Neither
operation skips ahead or restarts a different phase. A fresh process can
resume using the task row alone; it does not reconstruct origin from audit
history. A missing, unknown, terminal, or suspended origin raises
`InvalidResumeTarget`. An active row with a noncanonical phase raises
`InvalidTaskPhase`, rather than guessing how to reinterpret Phase 1 fixture
data or externally modified rows.

No migration, new column, config field, or audit hash-format change is needed.
`config_json` remains untouched. Arbitrary independent subphases are not
part of this phase's state model.

## API and guards

```python
from code_slayer.core import TaskState, TaskStateMachine

machine = TaskStateMachine(conn)
updated = machine.transition(
    task.task_id,
    expected_state=TaskState.CREATED,
    to_state=TaskState.INSPECTING,
    reason="task accepted for inspection",
)
```

State arguments must be `TaskState` enum members and the reason must be a
non-empty string. Optional actor attribution uses the foundation audit API.

- `CHECKPOINTED → COMPLETED` requires `completion_decision=True`.
- Entering `FAILED` requires `failure_decision=True`. This records an
  explicit terminal decision; it does not implement or claim policy approval.
- Resolving `BLOCKED` requires `reconciled_target=to_state`. This is the
  caller's explicit reconciliation decision, not an implemented reconciler.
- Misplaced decision inputs are rejected, including truthy non-booleans.
- Terminal tasks always reject transitions when the expected state matches.

Additional guards can be supplied at construction as pure callables taking
the frozen `(Task, TransitionRequest)` and raising `StateMachineError` to
reject a request. They run after graph validation, before writes, within
the same transaction. No external actions or mutations belong in guards.

Domain errors are exported from `core`: `InvalidTransition`,
`StaleTaskState`, `InvalidResumeTarget`, `InvalidTaskPhase`, and
`TerminalTaskError`, all under `StateMachineError`. A missing task retains
the repository's `KeyError` contract.

## Transaction, concurrency, and duplicate semantics

One `BEGIN IMMEDIATE` covers loading the current task, checking the mandatory
`expected_state`, graph/guard validation, updating state/phase, and appending
the audit event. The repository's private composition helper does not begin
or commit transactions. SQLite's write lock remains held through commit.
The returned task is read inside that lock and represents this transition,
even if another writer subsequently advances the task.

Phase 3 adds `transition_in_transaction(task_id, request=...)` for explicit
composition of a baseline and its state transition. It applies the same
validation and guards, requires a caller-owned `BEGIN IMMEDIATE` transaction,
and never begins or commits one itself. The caller must roll back the whole
transaction on failure. `transition()` uses this same validation path.

Two writers that observed `PLANNED` and request incompatible targets cannot
both win against that original state. The second writer checks its
expectation after acquiring the lock and raises `StaleTaskState`; it does
not reinterpret the request against the winner's new state. Existing
SQLite busy-timeout behavior still applies to prolonged lock contention.

Duplicate semantics are **explicit errors**, not success-shaped no-ops:

- Repeating a successful request with its original expected state raises
  `StaleTaskState`.
- Requesting a self-edge with the current expected state raises
  `InvalidTransition` (or `TerminalTaskError` for a terminal state).
- Rejected requests do not change timestamps, phase, state, or audit.

This is expected-state comparison, not a global request-ID ledger or a
revision fence: if a task later cycles back to the same state, equality
alone cannot identify an old request. Callers must issue transitions from
fresh observations; request replay across whole cycles is deferred.

Each real change appends one `STATE_TRANSITION` with `from_state`, `to_state`,
`reason`, `phase_before`, and `phase_after`, plus `resume_origin` when entering
or leaving a suspended state (including terminal failure from suspension).
The Phase 1 `from_phase`/`to_phase` keys remain as compatibility aliases.
Audit timestamps match the row's `updated_at`; existing canonical hashing,
sequence allocation, and append-only protections are unchanged.

## Evidence and boundaries

`tests/unit/test_state_machine.py` independently enumerates the approved
graph and checks all 225 state pairs. It also covers every origin and exact
phase round-trip, invalid origins and guard inputs, repair/checkpoint loops,
duplicates, actor attribution, extension guard rejection, and rollback
after the row update and after the audit append. Database triggers separately
force failures of either write.

`tests/integration/test_state_machine_recovery.py` covers separate SQLite
writers, reopening and resuming every active origin, and nine subprocess
crashes: after row update, after audit insertion before commit, and after
commit, each for verification, interruption, and blocking. The subprocess
uses the real state-machine API and `os._exit`, with no cleanup. Reopening
must show exactly the old row/audit or the complete new row/audit, preserve
the hash chain, and permit another legal transition. A separate connection
also verifies that uncommitted writes are invisible.

This layer supplies state semantics only. Adapters, model integration,
tools, policy, checkpoint and lease managers, scheduling, orchestration,
daemon/CLI transition commands, WebUI, environments, knowledge, and training
remain deferred. State names alone do not prove later-phase work occurred.
