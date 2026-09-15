"""`LocalWorkerRunner`: the persistent, resumable application service
composing every already-implemented Phase 1-7.6 boundary into one
provider-neutral runner (Phase 7.7 — `docs/ROADMAP.md
#local-worker-runtime`).

## Composition, not a new authority

Every step below is an *existing*, independently-tested boundary; this
module only sequences them and adds the durable bookkeeping (`runner_runs`
/`runner_human_resolutions`, schema v5) needed to survive a process
restart at any point:

```
exact original prompt
      |
      v
persistent run/control record (this module, control-plane database)
      |
      v
PromptAnalyst.analyze()                     (workers.prompt_analysis)
      |
      v
QuestionGate.evaluate()                     (workers.question_gate, hardened)
      |
      +-- ASK -> durable BLOCKED_ON_QUESTIONS -> STOP (no inference, no tool)
      |
      +-- SUPPRESS
              |
              v
      repository/task setup                (InspectionService, TaskStateMachine)
              |
              v
      control-plane trust check            (workers.trust.WorkerTrustManager)
              |
              v
      managed job worktree, if isolation
      is required                          (repo.job_worktree)
              |
              v
      lease/fencing                        (lease.manager.LeaseManager)
              |
              v
      execute_guarded_turn()               (workers.execution — protocol
              |                              validation, PolicyEngine,
              |                              ToolExecutor, durable evidence)
              v
      COMPLETED / DENIED_TRUST / FAILED / INTERRUPTED_RESUMABLE
```

No step here silently bypasses an existing boundary: `PolicyEngine`,
`ToolExecutor`, lease fencing, and `QuestionGate`'s hardened authority
model are all reused completely unmodified. This module never mutates
files, never executes a tool itself, and never grants trust — it only
decides *when* to call the things that already can.

## Control plane vs. execution plane

The **control plane** is one persistent database at the *primary*
repository's own worktree state directory (`store.location`,
`repo.identity`) — durable worker registration, conformance runs, trust
history (Phase 7.2/7.3), and every `runner_runs`/
`runner_human_resolutions` row this module writes. It is opened once,
for the life of a `LocalWorkerRunner` instance, and is never copied into
a job worktree's own database.

The **execution plane** is wherever a specific run's task/lease/tool
operations/checkpoints actually live: the control database itself, for
an ordinary read-only run that needs no isolation, or a managed job
worktree's own separate `state.db` (`repo.job_worktree`), for a run
whose declared tool policy includes mutating capability. `execute_
guarded_turn()`'s `trust_conn` parameter (Phase 7.7) is exactly what
makes this split possible: execution evidence (`ToolExecutor`,
`AuditWriter`) goes to the execution-plane connection, while every trust
read/write goes to the control-plane connection — never copied, never
duplicated.

## Authoritative resolution evidence: never from the analyst

`QuestionGate.evaluate()` only ever suppresses an ambiguity given a
`workers.question_gate.ResolutionEvidence` explicitly bound to its exact
id. This module is the one place such evidence may legitimately be
constructed, from exactly three producer categories
(`docs/CODE_SLAYER_VISION.md` §34):

- **A. `ORIGINAL_PROMPT`** — `record_user_resolution(..., source=
  EvidenceSource.ORIGINAL_PROMPT)`: a human/application decision that
  explicitly cites the original prompt's own text as the binding answer.
- **B. `REPOSITORY`/`RUNTIME`** — not yet automated in this phase (see
  "Known limitations" below): binding a real repository/runtime fact to
  a *specific* ambiguity id requires understanding what the ambiguity is
  actually asking, which is repository-context intelligence this phase
  deliberately does not implement (`docs/ROADMAP.md` Phase 8 boundary).
- **C. `DURABLE_TASK_EVIDENCE`** — `record_user_resolution(..., source=
  EvidenceSource.DURABLE_TASK_EVIDENCE)` (the default): an explicit
  human answer, durably recorded as this run's own prior decision.

The Prompt Analyst may only ever *suggest* — `Ambiguity.evidence_keys`/
`resolved_by_prompt_substring` — never manufacture. This module never
promotes an analyst's own hint into `ResolutionEvidence` on its behalf.

## Ownership and terminal lifecycle (Phase 7.7a)

`READY -> RUNNING` is an atomic claim. RUNNING is owned, not evidence of a
crash. Every executing invocation acquires a fresh lease session; a resume
caller never reconstructs or renews another caller's handle. Takeover uses
Phase-6 TTL, quiescence, process identity and child liveness unchanged.
Only after acquiring a new epoch does recovery inspect ALL task operations
and task state. Any operation, even FAILED or SUCCEEDED with no runner-side
operation id, prevents replay of the bounded turn.

Ordinary bounded read-only completions use the state machine's conditional
`IMPLEMENTING -> COMPLETED` edge, explicitly marked in task configuration
and guarded by a resolved, read-only journal. Failure/denial uses FAILED.
Task terminalization, fenced release, runner result and audit commit in one
transaction for ordinary runs. A separate job database commits its task and
release first; interruption before control bookkeeping is fail-closed and
never acquires a lease again for that terminal task. No checkpoint or
primary promotion is fabricated to finish a read-only turn.

## Known limitations (see also `docs/ROADMAP.md`'s Phase 7/8 boundary)

- A crash before a durable task/lease ownership link requires explicit
  reconciliation. A concurrent caller cannot distinguish that crash from
  live bootstrap, so it returns RUNNING without executing or changing the
  owner's record. ALIVE/UNKNOWN liveness similarly refuses takeover.
- **Mid-turn crash recovery is fail-closed, not fully automatic.** If a
  process crashes after `ToolExecutor` durably recorded `SUCCEEDED` but
  before the worker's continuation completed, `resume()` recovers
  cleanly only when nothing durable happened at all yet (safe to retry
  the whole bounded turn). When a tool operation already completed as
  part of the crashed attempt, this phase does not attempt to replay
  only the continuation step (which `execute_guarded_turn()`'s current,
  monolithic shape does not expose a clean way to do without a second
  tool-execution path) — it fails closed to `INTERRUPTED_RESUMABLE`
  instead of guessing or duplicating the effect.
- **No automatic checkpoint on completion.** No real worker currently
  holds mutating trust, so no run in this phase ever reaches a
  successful mutating completion that would need one; wiring
  `repo.checkpoint.CheckpointManager` into a real completion path is
  deferred until that becomes possible.
- **No automatic repository/runtime fact binding (producer B above).**
  Only explicit human/application resolutions are constructed
  automatically today.
- **No promotion into the primary repository, ever.** A job worktree's
  checkpoints (when they exist) live only under their own dedicated ref
  namespace; nothing in this module fast-forwards, merges, or
  cherry-picks into the primary worktree.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.core.transitions import TransitionRequest
from code_slayer.lease.manager import LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.repo import identity
from code_slayer.repo import job_worktree as job_worktree_module
from code_slayer.repo.baseline import InspectionService
from code_slayer.store import db as db_module
from code_slayer.store import location
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus
from code_slayer.store.models import RunnerRun
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.task_repo import TaskAlreadyActiveError, TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo
from code_slayer.workers.execution import execute_guarded_turn
from code_slayer.workers.prompt_analysis import EvidenceSource, PromptAnalysis, PromptAnalyst
from code_slayer.workers.prompt_provenance import (
    read_original_prompt,
    read_prompt_analysis,
    record_prompt_analysis,
)
from code_slayer.workers.protocol import WorkerAdapter
from code_slayer.workers.question_gate import (
    GateDecision,
    QuestionGate,
    ResolutionEvidence,
    ResolutionKind,
)
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager

# The one representative mutating capability this runner pre-checks
# control-plane trust for when a run's declared tool policy requires
# mutation (see the module docstring's "Known limitations"). Extending
# this to an arbitrary requested-capability set is future work; today
# every real worker is LOCKED for every mutating capability regardless
# (`workers.promotion` never promotes one), so the exact name checked
# does not change today's actual, observable outcome.
_MUTATION_PROBE_CAPABILITY = "write_file"

FINAL_TEXT_EVIDENCE_KIND = "runner_final_text"
HUMAN_ANSWER_EVIDENCE_KIND = "runner_human_answer"

_HUMAN_RESOLUTION_SOURCES = frozenset({
    EvidenceSource.ORIGINAL_PROMPT, EvidenceSource.DURABLE_TASK_EVIDENCE,
})


class RunStatus(StrEnum):
    """Application-level run status — layered above, and never a
    replacement for, `core.states.TaskState`. A run may exist in
    `ANALYZING`/`BLOCKED_ON_QUESTIONS`/`READY` before any `tasks` row
    exists at all."""

    ANALYZING = "ANALYZING"
    BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    DENIED_TRUST = "DENIED_TRUST"
    FAILED = "FAILED"
    INTERRUPTED_RESUMABLE = "INTERRUPTED_RESUMABLE"


_TERMINAL_STATUSES_NO_NEW_WORK = frozenset({
    RunStatus.COMPLETED.value, RunStatus.DENIED_TRUST.value, RunStatus.FAILED.value,
})


@dataclass(frozen=True)
class RunResult:
    """A structured result suitable for a future CLI/WebUI — never
    internal SQLite/connection details. `evidence_refs` and `reason` are
    diagnostic; `questions` is populated only for `BLOCKED_ON_QUESTIONS`."""

    run_id: str
    status: RunStatus
    task_id: str | None = None
    questions: tuple[str, ...] = ()
    reason: str | None = None
    final_text: str | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass
class _ExecutionPlane:
    conn: sqlite3.Connection
    blobs_dir: Path
    task: object
    _owns_conn: bool

    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()


class LocalWorkerRunner:
    """One persistent runner bound to exactly one primary repository.
    Construct fresh (a new instance, new connections) after any process
    restart — nothing about resuming a run relies on Python object
    memory from a prior instance; see `resume()`."""

    def __init__(
        self, primary_repo_path: Path | str, *, state_root_override: str | Path | None = None,
    ) -> None:
        self._primary = identity.resolve(primary_repo_path)
        self._state_root_override = state_root_override
        self._control_db_path = location.db_path(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        self._control_blobs_dir = location.blobs_dir(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        location.ensure_dirs(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        self._control_conn = db_module.connect(self._control_db_path)
        db_module.migrate(self._control_conn)

    def close(self) -> None:
        """Close the control-plane connection. A fresh `LocalWorkerRunner`
        instance against the same primary repository reopens the exact
        same durable state — nothing here is held only in memory."""
        self._control_conn.close()

    # -- audit ----------------------------------------------------------

    def _audit(self, run_id: str, event_type: EventType, payload: dict) -> None:
        AuditWriter(self._control_conn).append(
            task_id=None, event_type=event_type, actor_type="system",
            actor_id="local-worker-runner", payload={"run_id": run_id, **payload},
        )

    # -- public API -------------------------------------------------------

    def start(
        self, *, original_prompt: str, worker_id: str, role: str,
        prompt_analyst: PromptAnalyst, requires_mutation: bool = False,
        resolutions: tuple[ResolutionEvidence, ...] = (), adapter: WorkerAdapter | None = None,
    ) -> RunResult:
        """Create a new run and carry it as far as it can safely go
        without a caller-supplied `adapter`: through Prompt Analyst and
        Question Gate evaluation, to `BLOCKED_ON_QUESTIONS` or `READY`.
        If `adapter` is given and the gate suppresses, proceeds all the
        way through worker execution in this same call."""
        if not isinstance(original_prompt, str):
            raise TypeError("original_prompt must be a str")
        run_id = uuid.uuid4().hex
        now = utcnow_iso()
        store = ContentStore(self._control_conn, self._control_blobs_dir)
        with transaction(self._control_conn):
            prompt_blob = store.put(
                original_prompt.encode("utf-8"), media_type="text/plain",
                source_kind="original_prompt", exportable=False,
            )
            RunnerRepo(self._control_conn).create_in_transaction(
                run_id=run_id, created_at=now, repo_id=self._primary.repo_id,
                primary_worktree_id=self._primary.worktree_id,
                original_prompt_hash=prompt_blob.content_hash, worker_id=worker_id, role=role,
                requires_mutation=requires_mutation, status=RunStatus.ANALYZING.value,
            )
            self._audit(run_id, EventType.RUN_STARTED, {
                "original_prompt_hash": prompt_blob.content_hash, "worker_id": worker_id,
                "role": role, "requires_mutation": requires_mutation,
            })

        analysis = prompt_analyst.analyze(original_prompt, {})
        run = RunnerRepo(self._control_conn).get(run_id)
        return self._evaluate_gate(run, original_prompt, analysis, resolutions, adapter)

    def status(self, run_id: str) -> RunResult:
        """Read-only: the run's current durable state. Never mutates
        anything, never invokes a worker or tool."""
        return self._to_result(RunnerRepo(self._control_conn).get(run_id))

    def record_user_resolution(
        self, run_id: str, ambiguity_id: str, answer: str, *,
        resolution_kind: ResolutionKind,
        source: EvidenceSource = EvidenceSource.DURABLE_TASK_EVIDENCE,
    ) -> None:
        """The smallest explicit application API for answering one
        blocked ambiguity — an application/user action, never something
        the Prompt Analyst can trigger itself. Persists the answer
        durably (content-addressed), audits it, and constructs trusted
        `ResolutionEvidence` bound to exactly `ambiguity_id` — never a
        wildcard, never inferred. Never grants worker trust or tool
        permission: this call touches only `content_blobs`/
        `runner_human_resolutions`/`audit_events`.

        `source` must be `ORIGINAL_PROMPT` (this answer explicitly cites
        the original prompt's own text) or `DURABLE_TASK_EVIDENCE`
        (an explicit human/application decision, the default) — never
        `REPOSITORY`/`RUNTIME` (those are for deterministic Code-Slayer
        inspection facts, not human answers) and never a bare boolean
        "this is authorized": `resolution_kind` (`FACT` vs
        `AUTHORIZATION`) must be stated explicitly every time.
        """
        if source not in _HUMAN_RESOLUTION_SOURCES:
            raise ValueError(
                "a human resolution's source must be ORIGINAL_PROMPT or DURABLE_TASK_EVIDENCE"
            )
        if not isinstance(resolution_kind, ResolutionKind):
            raise TypeError("resolution_kind must be a ResolutionKind")
        RunnerRepo(self._control_conn).get(run_id)  # KeyError if the run does not exist
        now = utcnow_iso()
        store = ContentStore(self._control_conn, self._control_blobs_dir)
        with transaction(self._control_conn):
            blob = store.put(
                answer.encode("utf-8"), media_type="text/plain",
                source_kind=HUMAN_ANSWER_EVIDENCE_KIND, exportable=False,
            )
            RunnerRepo(self._control_conn).record_human_resolution_in_transaction(
                run_id=run_id, ambiguity_id=ambiguity_id, source=source.value,
                resolution_kind=resolution_kind.value, answer_content_hash=blob.content_hash,
                created_at=now,
            )
            self._audit(run_id, EventType.RUN_USER_RESOLUTION_RECORDED, {
                "ambiguity_id": ambiguity_id, "resolution_kind": resolution_kind.value,
                "source": source.value, "answer_content_hash": blob.content_hash,
            })

    def resume(
        self, run_id: str, *, adapter: WorkerAdapter | None = None,
        resolutions: tuple[ResolutionEvidence, ...] = (),
    ) -> RunResult:
        """Derive the next safe action entirely from durable state —
        never from Python object memory. Idempotent for a run already in
        a terminal status: returns it unchanged, invoking no worker and
        no tool. Concurrency-safe: claims the `READY -> RUNNING`
        transition inside one `BEGIN IMMEDIATE` transaction, so two
        concurrent `resume()` calls can never both execute the same
        bounded turn (see `_claim_for_execution()`)."""
        run = RunnerRepo(self._control_conn).get_or_none(run_id)
        if run is None:
            return RunResult(run_id=run_id, status=RunStatus.FAILED, reason="unknown_run")

        if run.status in _TERMINAL_STATUSES_NO_NEW_WORK:
            return self._to_result(run)

        if run.status == RunStatus.BLOCKED_ON_QUESTIONS.value:
            original_prompt = read_original_prompt(
                self._control_conn, self._control_blobs_dir, run.original_prompt_hash,
            )
            analysis = read_prompt_analysis(
                self._control_conn, self._control_blobs_dir, run.analysis_content_hash,
            )
            self._audit(run_id, EventType.RUN_RESUMED, {"from_status": run.status})
            return self._evaluate_gate(run, original_prompt, analysis, resolutions, adapter)

        if run.status == RunStatus.READY.value:
            claimed = self._claim_for_execution(run_id)
            if claimed is None:
                return self._to_result(RunnerRepo(self._control_conn).get(run_id))
            self._audit(run_id, EventType.RUN_RESUMED, {"from_status": run.status})
            return self._proceed_to_execution(claimed, adapter)

        if run.status == RunStatus.RUNNING.value:
            self._audit(run_id, EventType.RUN_RESUMED, {"from_status": run.status})
            return self._recover_mid_turn(run, adapter)

        # INTERRUPTED_RESUMABLE: requires explicit reconciliation this
        # phase does not automate (see the module docstring's "Known
        # limitations") -- returned unchanged, never guessed forward.
        return self._to_result(run)

    # -- gate evaluation --------------------------------------------------

    def _evaluate_gate(
        self, run: RunnerRun, original_prompt: str, analysis: PromptAnalysis,
        resolutions: tuple[ResolutionEvidence, ...], adapter: WorkerAdapter | None,
    ) -> RunResult:
        """Run (or re-run, on resume) `QuestionGate.evaluate()` against
        `analysis` — freshly produced by `PromptAnalyst.analyze()` when
        called from `start()`, or reconstructed from durable evidence via
        `workers.prompt_provenance.read_prompt_analysis()` when called
        from `resume()` on a `BLOCKED_ON_QUESTIONS` run (the analyst is
        never re-invoked merely to continue). `resolutions` is combined
        with every durably recorded human resolution for this run — see
        `_load_human_resolutions()`."""
        all_resolutions = tuple(resolutions) + self._load_human_resolutions(run.run_id)
        gate_result = QuestionGate().evaluate(
            original_prompt=original_prompt, analysis=analysis, resolutions=all_resolutions,
        )
        provenance = record_prompt_analysis(
            self._control_conn, self._control_blobs_dir, task_id=run.task_id,
            analysis=analysis, gate_result=gate_result,
        )
        now = utcnow_iso()
        if gate_result.decision == GateDecision.ASK:
            with transaction(self._control_conn):
                updated = RunnerRepo(self._control_conn).update_in_transaction(
                    run.run_id, updated_at=now, status=RunStatus.BLOCKED_ON_QUESTIONS.value,
                    analysis_content_hash=provenance.analysis_content_hash,
                    questions_json=json.dumps(list(gate_result.questions)),
                    reason="blocked_on_questions",
                )
                self._audit(run.run_id, EventType.RUN_BLOCKED, {
                    "questions": list(gate_result.questions), "reasons": list(gate_result.reasons),
                })
            return self._to_result(updated)

        with transaction(self._control_conn):
            updated = RunnerRepo(self._control_conn).update_in_transaction(
                run.run_id, updated_at=now, status=RunStatus.READY.value,
                analysis_content_hash=provenance.analysis_content_hash,
                questions_json=None, reason="suppressed",
            )
        if adapter is None:
            return self._to_result(updated)
        claimed = self._claim_for_execution(run.run_id)
        if claimed is None:
            return self._to_result(RunnerRepo(self._control_conn).get(run.run_id))
        return self._proceed_to_execution(claimed, adapter)

    def _load_human_resolutions(self, run_id: str) -> tuple[ResolutionEvidence, ...]:
        rows = RunnerRepo(self._control_conn).latest_human_resolutions(run_id)
        return tuple(
            ResolutionEvidence(
                key=f"human:{row.ambiguity_id}", source=EvidenceSource(row.source),
                resolution_kind=ResolutionKind(row.resolution_kind),
                resolves_ambiguity_ids=(row.ambiguity_id,),
            )
            for row in rows
        )

    # -- concurrency-safe claim ---------------------------------------------

    def _claim_for_execution(self, run_id: str) -> RunnerRun | None:
        """Atomically transition `READY -> RUNNING` inside one `BEGIN
        IMMEDIATE` transaction. `None` if the run was not (or no longer)
        `READY` — a concurrent caller already claimed it, or it moved on
        its own. RUNNING recovery additionally requires a fresh Phase-6
        lease epoch; it must never reconstruct the active owner's handle."""
        with transaction(self._control_conn):
            current = RunnerRepo(self._control_conn).get_or_none(run_id)
            if current is None or current.status != RunStatus.READY.value:
                return None
            return RunnerRepo(self._control_conn).update_in_transaction(
                run_id, updated_at=utcnow_iso(), status=RunStatus.RUNNING.value,
            )

    # -- execution plane ----------------------------------------------------

    def _setup_execution_plane(self, run: RunnerRun) -> _ExecutionPlane:
        if run.requires_mutation:
            handle = job_worktree_module.create_job_worktree(
                self._primary.repo_root, state_root_override=self._state_root_override,
            )
            conn = db_module.connect(handle.db_path)
            execution_worktree_id = handle.worktree_id
            job_worktree_path = str(handle.path)
            repo_root = str(handle.path)
            blobs_dir = handle.blobs_dir
            owns_conn = True
        else:
            conn = self._control_conn
            execution_worktree_id = self._primary.worktree_id
            job_worktree_path = None
            repo_root = str(self._primary.repo_root)
            blobs_dir = self._control_blobs_dir
            owns_conn = False

        task = TaskRepo(conn).create(
            description=f"runner run {run.run_id}", repo_root=repo_root,
            repo_id=self._primary.repo_id, worktree_id=execution_worktree_id,
            task_id=run.run_id, config={
                "tool_policy": {"scope": ["."]},
                "execution_kind": "bounded_read_only_turn" if not run.requires_mutation else "job",
            },
        )
        service = InspectionService(conn, blobs_dir=blobs_dir)
        service.start(task.task_id)
        service.capture(task.task_id)
        machine = TaskStateMachine(conn)
        for to_state, expected in (
            (TaskState.PLANNING, TaskState.BASELINED),
            (TaskState.PLANNED, TaskState.PLANNING),
            (TaskState.IMPLEMENTING, TaskState.PLANNED),
        ):
            machine.transition(
                task.task_id, expected_state=expected, to_state=to_state,
                reason="runner_bootstrap",
            )

        with transaction(self._control_conn):
            RunnerRepo(self._control_conn).update_in_transaction(
                run.run_id, updated_at=utcnow_iso(), task_id=task.task_id,
                execution_worktree_id=execution_worktree_id, job_worktree_path=job_worktree_path,
            )
        return _ExecutionPlane(
            conn=conn, blobs_dir=blobs_dir, task=TaskRepo(conn).get(task.task_id),
            _owns_conn=owns_conn,
        )

    def _reopen_execution_plane(self, run: RunnerRun) -> _ExecutionPlane:
        """Reconstruct the exact same execution plane a prior call
        already set up, entirely from durable `RunnerRun` fields --
        never from Python object memory, and never a second job worktree
        merely because a prior connection was closed."""
        if run.job_worktree_path is not None:
            job_db_path = location.db_path(
                self._primary.repo_id, run.execution_worktree_id,
                override=self._state_root_override,
            )
            conn = db_module.connect(job_db_path)
            blobs_dir = location.blobs_dir(
                self._primary.repo_id, run.execution_worktree_id,
                override=self._state_root_override,
            )
            owns_conn = True
        else:
            conn = self._control_conn
            blobs_dir = self._control_blobs_dir
            owns_conn = False
        task = TaskRepo(conn).get(run.task_id)
        return _ExecutionPlane(conn=conn, blobs_dir=blobs_dir, task=task, _owns_conn=owns_conn)

    def _acquire_lease(self, execution: _ExecutionPlane, run: RunnerRun):
        # Session identity belongs to this invocation, never to the durable run.
        # Only the returned handle confers ownership; resume cannot renew an
        # epoch reconstructed from a row belonging to another caller.
        return LeaseManager(execution.conn).acquire(
            worktree_id=execution.task.worktree_id, task_id=execution.task.task_id,
            worker_id=run.worker_id, worker_session_id=uuid.uuid4().hex,
        )

    def _proceed_to_execution(self, run: RunnerRun, adapter: WorkerAdapter | None) -> RunResult:
        """`run.status` is already `RUNNING` (claimed) here. Sets up (or
        reuses) the execution plane, acquires a fresh task lease, checks
        control-plane trust for a mutating run before inference, and runs
        exactly one bounded `execute_guarded_turn()` call. If `adapter` is
        `None`, rolls the claim back to
        `READY` without spending any inference — a caller may legitimately
        want to stop there."""
        if adapter is None:
            with transaction(self._control_conn):
                updated = RunnerRepo(self._control_conn).update_in_transaction(
                    run.run_id, updated_at=utcnow_iso(), status=RunStatus.READY.value,
                )
            return self._to_result(updated)

        original_prompt = read_original_prompt(
            self._control_conn, self._control_blobs_dir, run.original_prompt_hash,
        )
        try:
            execution = (
                self._setup_execution_plane(run) if run.task_id is None
                else self._reopen_execution_plane(run)
            )
        except TaskAlreadyActiveError as exc:
            # INV-2 (at most one non-terminal task per worktree) already
            # refused this -- a non-isolated run shares the primary
            # worktree's own single task/lease slot, so another
            # currently-active task there safely blocks this one exactly
            # like a held lease would; never stolen, never guessed past.
            return self._finish(
                run, RunStatus.INTERRUPTED_RESUMABLE, f"execution_plane_unavailable:{exc}",
            )
        try:
            run = RunnerRepo(self._control_conn).get(run.run_id)
            lease_result = self._acquire_lease(execution, run)
            if lease_result.decision != Decision.ALLOW:
                return self._finish(
                    run, RunStatus.INTERRUPTED_RESUMABLE,
                    f"lease_unavailable:{lease_result.reason}",
                )
            return self._execute_owned(
                run, execution, lease_result.handle, adapter, original_prompt,
            )
        finally:
            execution.close()

    def _execute_owned(self, run, execution, lease, adapter, original_prompt) -> RunResult:
        # Apply the journal veto to every entry path, including a READY row
        # that unexpectedly already has execution evidence. The runner's own
        # status and nullable operation pointer are never replay authority.
        operations = ToolOperationsRepo(execution.conn).list_for_task(run.task_id)
        task = TaskRepo(execution.conn).get(run.task_id)
        reason = None
        if any(op.status in OperationStatus.UNRESOLVED for op in operations):
            reason = "unresolved_tool_operation_requires_reconciliation"
        elif operations or run.tool_operation_id is not None:
            reason = "mid_turn_recovery_not_supported_tool_already_executed"
        elif task.state != TaskState.IMPLEMENTING.value:
            reason = "execution_task_requires_reconciliation"
        if reason is not None:
            return self._finish(run, RunStatus.INTERRUPTED_RESUMABLE, reason)
        if run.requires_mutation:
            trust_level = WorkerTrustManager(self._control_conn).current_trust(
                run.worker_id, run.role, _MUTATION_PROBE_CAPABILITY,
            )
            if trust_level not in (TrustLevel.GUARDED, TrustLevel.AUTO):
                return self._finish_owned(
                    run, execution, lease, RunStatus.DENIED_TRUST,
                    f"denied_trust:{_MUTATION_PROBE_CAPABILITY}:{trust_level.value}",
                )
        outcome = execute_guarded_turn(
            execution.conn, adapter, task_id=execution.task.task_id, worker_id=run.worker_id,
            role=run.role, original_prompt=original_prompt, lease=lease,
            blobs_dir=execution.blobs_dir, trust_conn=self._control_conn,
        )
        tool_operation_id = outcome.tool_result.operation_id if outcome.tool_result else None
        status = (
            RunStatus.COMPLETED if outcome.ok else
            RunStatus.DENIED_TRUST if outcome.reason.startswith("trust_denied")
            else RunStatus.FAILED
        )
        return self._finish_owned(
            run, execution, lease, status, outcome.reason,
            final_text=(outcome.final_text or "") if outcome.ok else None,
            tool_operation_id=tool_operation_id,
        )

    def _finish_owned(self, run, execution, lease, status, reason, **kwargs) -> RunResult:
        """Terminalize and release atomically, only with this caller's real handle.

        Unknown effects keep the non-terminal task and lease occupied. Execution
        finalization precedes control bookkeeping: a crash between the databases
        leaves a terminal task that recovery will never execute again.
        """
        manager = LeaseManager(execution.conn)
        with transaction(execution.conn):
            current = LeaseRepo(execution.conn).get(lease.worktree_id)
            if (
                current is None or current.task_id != run.task_id
                or (current.worker_id, current.worker_session_id, current.generation)
                != (lease.worker_id, lease.worker_session_id, lease.generation)
                or current.status not in (LeaseStatus.ACTIVE, LeaseStatus.QUIESCING)
            ):
                # Losing authority also forbids overwriting the new owner's run result.
                return self._to_result(RunnerRepo(self._control_conn).get(run.run_id))
            if ToolOperationsRepo(execution.conn).list_unresolved(task_id=run.task_id):
                status = RunStatus.INTERRUPTED_RESUMABLE
                reason = "unresolved_tool_operation_requires_reconciliation"
            else:
                task = TaskRepo(execution.conn).get(run.task_id)
                target = TaskState.COMPLETED if status == RunStatus.COMPLETED else TaskState.FAILED
                TaskStateMachine(execution.conn).transition_in_transaction(
                    task.task_id, request=TransitionRequest(
                        expected_state=TaskState(task.state), to_state=target,
                        reason=f"runner:{status.value}:{reason}",
                        completion_decision=target == TaskState.COMPLETED,
                        failure_decision=target == TaskState.FAILED,
                    ), actor_id="local-worker-runner",
                )
                released = manager.release_in_transaction(lease)
                if released.decision != Decision.ALLOW:
                    raise RuntimeError(f"fenced terminal release failed: {released.reason}")
            if execution.conn is self._control_conn:
                # Ordinary runs share one database: result, task, release and
                # their audit events commit together, with no terminal gap.
                return self._finish(run, status, reason, **kwargs)
        return self._finish(run, status, reason, **kwargs)

    def _recover_mid_turn(self, run: RunnerRun, adapter: WorkerAdapter | None) -> RunResult:
        """RUNNING means owned, not crashed. Prove takeover before any replay.

        A missing task/lease link is ambiguous (the first caller may still be
        bootstrapping). Refuse without changing its state. Established leases
        use Phase-6 TTL/quiescence/process-and-child liveness, with UNKNOWN
        denying takeover. Never renew somebody else's persisted identity.
        """
        if run.task_id is None or adapter is None:
            return self._to_result(run)
        execution = self._reopen_execution_plane(run)
        try:
            if execution.task.state in (TaskState.COMPLETED.value, TaskState.FAILED.value):
                # Execution-plane finalization already committed. Do not acquire
                # a new lease for a terminal task and occupy a reusable slot.
                with transaction(self._control_conn):
                    run = RunnerRepo(self._control_conn).get(run.run_id)
                    if run.status != RunStatus.RUNNING.value:
                        return self._to_result(run)
                    return self._finish(
                        run, RunStatus.INTERRUPTED_RESUMABLE,
                        "terminal_execution_task_requires_reconciliation",
                    )
            current = LeaseRepo(execution.conn).get(execution.task.worktree_id)
            if current is None or current.task_id != run.task_id:
                return self._to_result(RunnerRepo(self._control_conn).get(run.run_id))
            lease_result = self._acquire_lease(execution, run)
            if lease_result.decision != Decision.ALLOW:
                return self._to_result(RunnerRepo(self._control_conn).get(run.run_id))
            # The old owner may have completed between our initial read and
            # acquiring its released lease. Re-read both authoritative records.
            run = RunnerRepo(self._control_conn).get(run.run_id)
            task = TaskRepo(execution.conn).get(run.task_id)
            if run.status != RunStatus.RUNNING.value:
                LeaseManager(execution.conn).release(lease_result.handle)
                return self._to_result(run)
            if task.state in (TaskState.COMPLETED.value, TaskState.FAILED.value):
                LeaseManager(execution.conn).release(lease_result.handle)
                return self._finish(
                    run, RunStatus.INTERRUPTED_RESUMABLE,
                    "terminal_execution_task_requires_reconciliation",
                )
            original_prompt = read_original_prompt(
                self._control_conn, self._control_blobs_dir, run.original_prompt_hash,
            )
            return self._execute_owned(
                run, execution, lease_result.handle, adapter, original_prompt,
            )
        finally:
            execution.close()

    def _finish(
        self, run: RunnerRun, status: RunStatus, reason: str, *,
        final_text: str | None = None, tool_operation_id: str | None | object = ...,
    ) -> RunResult:
        final_text_content_hash: str | None | object = ...
        if final_text is not None:
            store = ContentStore(self._control_conn, self._control_blobs_dir)
            blob = store.put(
                final_text.encode("utf-8"), media_type="text/plain",
                source_kind=FINAL_TEXT_EVIDENCE_KIND, exportable=False,
            )
            final_text_content_hash = blob.content_hash
        with (
            nullcontext() if self._control_conn.in_transaction else transaction(self._control_conn)
        ):
            updated = RunnerRepo(self._control_conn).update_in_transaction(
                run.run_id, updated_at=utcnow_iso(), status=status.value, reason=reason,
                final_text_content_hash=final_text_content_hash,
                tool_operation_id=tool_operation_id, questions_json=None,
            )
            self._audit(run.run_id, EventType.RUN_FINISHED, {
                "status": status.value, "reason": reason, "task_id": run.task_id,
            })
        return self._to_result(updated)

    def _to_result(self, run: RunnerRun) -> RunResult:
        questions = tuple(json.loads(run.questions_json)) if run.questions_json else ()
        final_text = None
        if run.final_text_content_hash is not None:
            final_text = ContentStore(self._control_conn, self._control_blobs_dir).read(
                run.final_text_content_hash,
            ).decode("utf-8")
        return RunResult(
            run_id=run.run_id, status=RunStatus(run.status), task_id=run.task_id,
            questions=questions, reason=run.reason, final_text=final_text,
        )
