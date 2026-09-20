# Autonomous Engineering Loop V1

Status: prototype vertical slice (P0 + core P1). Entry point:
`code_slayer.coding.pipeline.run_coding_job()`.

## What this is

A code-owned pipeline that takes an already-validated Planner plan
(`planning.models.PlanState.READY`) and drives it through:

```
Planner handoff (validated, fail-closed)
        |
        v
  isolated Coder workspace (repo.job_worktree, independently re-verified)
        |
        v
  bounded Coder tool-loop (coding.tool_loop) -- real, policy-checked
  mutations via the existing tools.executor.ToolExecutor/policy.engine.
  PolicyEngine, journaled as real store.models.ToolOperation rows
        |
        v
  independent mutation audit (coding.mutation_guard) -- real git status,
  never a model's claim; fails closed on any unauthorized path
        |
        v
  finalization.service.Finalizer -- real, isolated-tree verification
  commands (pytest/ruff/...), UNMODIFIED
        |
        +-- REPAIR_REQUIRED --> bounded Repairer (reuses the SAME
        |                        REPAIRING <-> VERIFYING FSM edge and
        |                        the SAME max_repair_attempts budget
        |                        Finalizer already enforces) --> loop back
        |
        v
  Reviewer (coding.reviewer) -- independent, non-mutating role, produces
  a real finalization.types.ReviewEvidence Finalizer consults
        |
        v
  Security (coding.security_gate) -- independent, non-mutating role;
  FAIL/HUMAN_REQUIRED blocks readiness, full stop
        |
        v
  READY_FOR_HUMAN_MERGE (task reaches CHECKPOINTED/COMPLETED; never
  merges, pushes, deploys, or mutates a branch)
```

## Why so little new code

Discovery (see the PR/commit history on this branch) found that CSLR
already has nearly the entire pipeline: `core.state_machine.
TaskStateMachine` already has `REVIEWING`/`REPAIRING` states wired in
anticipation of exactly this work; `repo.job_worktree` is already a
complete, hardened isolated-worktree-per-job abstraction; `tools.executor.
ToolExecutor`/`policy.engine.PolicyEngine` already enforce scope/
ownership/protected-paths on every mutation; `finalization.service.
Finalizer` already has a bounded repair loop and an unused `ReviewEvidence`
placeholder explicitly documented as "a stable, forward-compatible place
to accept review evidence later." This package fills in exactly the
missing pieces — a real multi-tool-call Coder mutation loop (`workers.
execution.execute_guarded_turn` is a single-tool, read-only conformance
harness, not a mutation engine), a real Reviewer, a real Security gate,
and the orchestration wiring them together — and reuses everything else
unmodified.

## Role boundaries (enforced by construction, not convention)

- **Coder** (`coding.tool_loop`): the only role that mutates. Offered
  exactly `read_file`/`create_file`/`write_file`/`apply_patch`
  (`CODER_TOOLS`) — never `run_command` (narrowly allow-listed to
  `git rev-parse` only, not a shell) or `checkpoint_create`. Every
  mutation goes through the real `ToolExecutor`, never a raw filesystem
  write. The model's own advisory report (`coding.contracts.
  CoderModelResult`, parsed by the closed-field `parse_coder_model_result
  ()`) can never become mutation evidence — `CoderAuthoritativeFacts` is
  built exclusively from real `store.models.ToolOperation` rows.
- **Reviewer**/**Security** (`coding.reviewer`/`coding.security_gate`):
  offered `allowed_tools=()` — a tool-call attempt is structurally
  `UNAUTHORIZED_CAPABILITY` before `PolicyEngine`/`ToolExecutor` are ever
  reached. They see a diff `coding.mutation_guard.compute_diff_text()`
  independently computes from real git state, never the Coder's own
  narrative. A closed-field parser (`parse_reviewer_model_result()`/
  `parse_security_model_result()`) is the only way a model's text can
  become a verdict.
- **Finalizer**: unmodified `finalization.service.Finalizer`. This
  package never re-implements verification or the repair-attempt bound —
  it calls the real thing, passing a real `ReviewEvidence`.
- No role can write `coding.pipeline_types.CodingJobState` to a success
  value, and no role can transition `core.states.TaskState` — only
  `coding.pipeline.run_coding_job()` (code) does either, via `store.
  coding_jobs_repo.CodingJobsRepo`/`core.state_machine.TaskStateMachine`.

## Security controls

- **Main-checkout immutability**: every mutation happens inside a
  disposable `repo.job_worktree` linked worktree, never the primary
  checkout. `tests/integration/test_coding_pipeline.py::
  test_primary_checkout_is_never_mutated` proves this with real
  before/after `git rev-parse HEAD`/`git status --porcelain`/filesystem
  snapshots.
- **Worktree isolation + preflight**: `coding.workspace.
  prepare_coder_workspace()` creates the worktree via the existing
  hardened `repo.job_worktree.create_job_worktree()`, then independently
  re-resolves (fresh `git rev-parse`) that the worktree really is pinned
  to the requested base commit and genuinely clean, before any model
  execution is authorized.
- **Path/mutation enforcement**: every write goes through the real
  `PolicyEngine` (mutation only in `IMPLEMENTING`/`REPAIRING`, scope-
  checked, protected-path-checked, ownership-checked) — never bypassed.
  `coding.mutation_guard.verify_authorized_mutations()` then
  independently re-inspects the actual on-disk state via real Git
  plumbing (`repo.job_worktree_git.worktree_status()`) and fails the job
  closed if anything changed outside what a real, journaled
  `ToolExecutor` operation accounts for.
- **Command policy**: the Coder loop offers no shell/command-execution
  capability at all. Verification commands run only through
  `finalization.verification`'s existing closed `_ARGV_BY_COMMAND`
  allow-list, against an isolated materialized tree, never the live
  worktree.
- **Staleness**: `coding.pipeline_types.diff_fingerprint()` fingerprints
  exactly the diff text a Reviewer/Security turn saw; each repair round
  recomputes a fresh diff and re-runs Reviewer before Finalizer is
  consulted again, so a stale verdict can never be reused after a
  mutation.
- **Bounded repair**: `CodingJobConfig.max_repair_attempts` (default 2)
  is passed straight into `Finalizer.decide_after_verification()`'s own,
  already-tested `max_repair_attempts` parameter — no second budget
  invented. Exhaustion (`Finalizer`'s own `repair_attempts_exhausted`
  reason) maps to `CodingJobState.HUMAN_REQUIRED`.
- **Security veto**: `SecurityVerdict.PASS` is the only verdict under
  which `coding.pipeline` ever calls `finalization.lifecycle.
  advance_ready_for_checkpoint()`/`advance_checkpointed_completion()`.
  `FAIL`/`HUMAN_REQUIRED` leave the task at `READY_FOR_CHECKPOINT`
  forever — never checkpointed, never completed.
- **Baseline Security Certification is untouched**: this package reviews
  one job's code change; it neither reads, writes, nor substitutes for
  `workers.security_baseline`'s mandatory, worker-level certification.
- **Residual risk — shared adapter across roles**: `run_coding_job()`
  accepts separate `coder_adapter`/`reviewer_adapter`/`security_adapter`
  parameters, but a caller may legitimately point all three at the same
  underlying model/process (this prototype's own example usage does).
  Capability-level independence is still real and enforced regardless
  (Reviewer/Security literally cannot call a mutation tool, whatever they
  are told to do), but *judgment*-level independence — a genuinely
  different, independently qualified model catching what the Coder's own
  model missed, including a prompt-injection attempt embedded in
  reviewed repository content — is only as strong as the adapters a
  caller actually supplies. A production deployment that wants real
  judgment independence must supply genuinely distinct, independently
  qualified adapters for each role; this slice does not implement or
  enforce that distinctness itself.

## Job lifecycle (`coding.pipeline_types.CodingJobState`)

`CREATED -> PREFLIGHT -> WORKSPACE_READY -> RUNNING -> VALIDATING ->
(IN_REPAIR <-> VALIDATING)* -> SECURITY_REVIEW -> READY_FOR_HUMAN_MERGE`,
or `BLOCKED`/`HUMAN_REQUIRED`/`FAILED` at any point. `READY_FOR_HUMAN_
MERGE` is the furthest state this pipeline may ever reach — it never
merges, pushes, deploys, restarts a service, or mutates live production
configuration, matching `AGENTS.md`'s non-negotiable rules.

## Persistence

One new table, `coding_jobs` (`store.migrations.0021_coding_jobs.sql`),
mirrors `planning_jobs`' own "identity locked at creation, lifecycle
fields evolve" shape (see that migration's own comment for why rich
per-attempt evidence lives in `audit_events` instead of a second ledger).
Two new `EventType` members (`CODING_SECURITY_REVIEW_DECIDED`,
`CODING_UNAUTHORIZED_MUTATION_DETECTED`) plus `CODING_JOB_CREATED`; every
other event reuses the existing vocabulary (`REVIEW_STARTED`/
`REVIEW_FINDING`/`REPAIR_STARTED`/`REPAIR_FINISHED`/
`FINALIZATION_DECIDED`, all already defined and, until now, unused).

## How to run the focused tests

```
.venv/bin/python -m pytest tests/unit/test_coding_*.py tests/integration/test_coding_*.py -q
```

## How to invoke the prototype

```python
from code_slayer.coding.pipeline import run_coding_job, CodingJobConfig

result = run_coding_job(
    "/path/to/repo",
    control_conn=control_conn, control_blobs_dir=control_blobs_dir,
    plan=ready_plan_row,  # planning.models.PlanState.READY
    original_prompt="...", allowed_scope=("src/mymodule",),
    coder_adapter=my_worker_adapter, reviewer_adapter=my_worker_adapter,
    security_adapter=my_worker_adapter,
    config=CodingJobConfig(max_repair_attempts=2),
)
print(result.final_state)  # CodingJobState.READY_FOR_HUMAN_MERGE, BLOCKED, HUMAN_REQUIRED, or FAILED
```

`coder_adapter`/`reviewer_adapter`/`security_adapter` each implement
`workers.protocol.WorkerAdapter` (`infer(WorkerRequest) -> WorkerResponse`)
— independent role identities; a real deployment may point each at a
different qualified model, though this prototype does not implement the
role-qualification/certification layer for Coder/Reviewer/Repairer/
Security (see Known Limitations in the final report).

## Known limitations

- No production wiring (CLI/API surface, worker registration for these
  roles, role qualification/certification for Coder/Reviewer/Repairer/
  Security) — this is the pipeline core only.
- `CoderContextEvidence` is passed empty; real repository-intelligence
  context (`intelligence.models.ProjectEvidence`/`ContextPack`) is not
  yet wired into a Coder turn's prompt.
- Repair evidence handed to a repaired Coder turn is a text summary of
  the Finalizer's reason code and Reviewer's summary, not a fully
  re-typed `finalization.types.VerificationCommandResult` tuple (the full
  structured evidence remains durably available in `audit_events`).
- Command/tool-call evidence audited at the job level is a count/summary,
  not the full per-call `ToolCallAttempt` history (also fully available
  from `run_coder_turn()`'s own return value to a caller that wants it).
