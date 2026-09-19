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

## Cloud escalation authorization: a separate question from QuestionGate (Phase 7.7e)

`start()`/`resume()`'s `cloud_escalation` parameter is unrelated to, and
never derived from, any `ResolutionEvidence`/human `QuestionGate`
resolution above — a human authorizing a destructive *action*
(`ResolutionKind.AUTHORIZATION`) says nothing about whether a *cloud*
worker may receive network transport at all, a completely separate
question. See `workers.cloud_escalation`'s own module docstring for the
full design; forwarded, unchanged, to `workers.execution.
execute_guarded_turn()`'s pre-transport gate.

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
- **A crash during `ANALYZING` requires explicit reconciliation, same as
  above.** An ordinary `PromptAnalystError` raised during `start()`'s own
  call is caught and terminalized to `FAILED` in that same call — a run
  is only ever still found at `ANALYZING` by `resume()` if the process
  crashed before that could happen. `resume()` never re-invokes the
  analyst itself (it takes no `prompt_analyst` argument, exactly like the
  `BLOCKED_ON_QUESTIONS` branch), so it fails closed to
  `INTERRUPTED_RESUMABLE` instead of guessing whether analysis actually
  completed.
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
from code_slayer.finalization.lifecycle import (
    CheckpointAdvanceOutcome,
    advance_checkpointed_completion,
    advance_ready_for_checkpoint,
)
from code_slayer.finalization.service import Finalizer, checkpointed_completion_guard
from code_slayer.finalization.verification import FinalizationError
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
from code_slayer.store.models import RunnerRun, Worker
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.task_repo import TaskAlreadyActiveError, TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.tools import file_tools as files
from code_slayer.workers.cloud_escalation import CloudEscalationAuthorization
from code_slayer.workers.execution import execute_guarded_turn
from code_slayer.workers.lifecycle import LifecycleTransitionResult
from code_slayer.workers.lifecycle import archive_worker as _lifecycle_archive_worker
from code_slayer.workers.lifecycle import reactivate_worker as _lifecycle_reactivate_worker
from code_slayer.workers.prompt_analysis import (
    EvidenceSource,
    PromptAnalysis,
    PromptAnalyst,
    PromptAnalystError,
)
from code_slayer.workers.prompt_provenance import (
    read_original_prompt,
    read_prompt_analysis,
    record_prompt_analysis,
)
from code_slayer.workers.protocol import (
    WorkerAdapter,
    WorkerSupplementalKind,
    WorkerSupplementalResolution,
    WorkerSupplementalSource,
)
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


@dataclass(frozen=True)
class _HumanResolution:
    """One durable `runner_human_resolutions` row, reconstructed with its
    identity/authority fields typed (Phase 7.7d) — the richer, in-process
    counterpart `_load_durable_human_resolutions()` builds so both the
    `QuestionGate` evidence view (`_resolutions_as_evidence()`) and the
    worker-facing supplemental-context view (`_build_supplemental_
    resolutions()`) are derived from exactly one durable read, never two
    different reconstructions that could disagree."""

    ambiguity_id: str
    source: EvidenceSource
    resolution_kind: ResolutionKind
    answer_content_hash: str


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

    def register_worker(self, *, worker_id: str, kind: str, network_class: str) -> Worker:
        """Idempotently register a worker in the control-plane `workers`
        table (`store.workers_repo.WorkersRepo`) — the exact row `api.
        service.ApplicationService.start()` requires to exist before a
        run can even be created, and the same row `workers.trust.
        WorkerTrustManager`/`workers.cloud_escalation` read from. Grants
        no trust and no capability by itself: a freshly registered
        worker starts with no conformance evidence and no trust history,
        exactly like any other newly registered worker."""
        return WorkersRepo(self._control_conn).register(
            worker_id=worker_id, kind=kind, network_class=network_class,
        )

    def archive_worker(self, worker_id: str) -> LifecycleTransitionResult:
        """H.3: thin pass-through to the one canonical lifecycle
        transition (`workers.lifecycle.archive_worker()`) against this
        runner's own PRODUCTION control connection -- exists so
        `api.admin.AdminFacade` can perform the administrative
        transition through the same `self._app.runner()` context
        manager it already uses for `register_worker()`, rather than
        opening a second connection to the same database. See that
        module's own docstring for the complete atomicity/policy
        contract; this method adds no policy of its own."""
        return _lifecycle_archive_worker(self._control_conn, worker_id=worker_id)

    def reactivate_worker(self, worker_id: str) -> LifecycleTransitionResult:
        """See `archive_worker()`'s own docstring."""
        return _lifecycle_reactivate_worker(self._control_conn, worker_id=worker_id)

    # -- audit ----------------------------------------------------------

    def _worker_archived(self, worker_id: str) -> bool:
        """H.3: `True` only for a worker that is REGISTERED and
        ARCHIVED. An unregistered `worker_id` is not this gate's
        concern (a run cannot durably exist for a worker that was
        never registered in the first place under this codebase's
        existing invariants) -- never treated as archived."""
        worker = WorkersRepo(self._control_conn).get(worker_id)
        return worker is not None and worker.lifecycle_state != "ACTIVE"

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
        cloud_escalation: CloudEscalationAuthorization | None = None,
    ) -> RunResult:
        """Create a new run and carry it as far as it can safely go
        without a caller-supplied `adapter`: through Prompt Analyst and
        Question Gate evaluation, to `BLOCKED_ON_QUESTIONS` or `READY`.
        If `adapter` is given and the gate suppresses, proceeds all the
        way through worker execution in this same call.

        A `PromptAnalystError` raised by `prompt_analyst.analyze()`
        (transport failure, non-conforming or malformed model output —
        see that exception's own docstring) never leaves this run
        durably stranded in `ANALYZING`: it is caught here and
        terminalized to `RunStatus.FAILED` via the same `_finish()` this
        module already uses for every other terminal outcome, exactly
        mirroring how `workers.execution.execute_guarded_turn()`'s own
        `transport_failure`/malformed-response outcomes already
        terminalize to `FAILED` rather than propagating a raw exception.
        This is a genuine analysis failure, never a qualification/trust
        result — it grants no trust, denies no trust, and creates no
        task; a fresh `start()` call with a new prompt/run is always
        still available. Nothing durable or irreversible had happened
        yet at this point (no task, no lease, no tool call), so this is
        always safe to terminalize outright, never `INTERRUPTED_
        RESUMABLE` (that status is reserved for a turn that may have
        left real execution-plane side effects requiring reconciliation
        — see `resume()`'s own handling of a run found at `ANALYZING`,
        the different, crash-shaped case where this exception was never
        actually raised because the process died before it could be).

        `cloud_escalation` (Phase 7.7e) is forwarded, unchanged, to
        `workers.execution.execute_guarded_turn()`'s own pre-transport
        gate if execution is reached this same call — see `workers.
        cloud_escalation`'s module docstring. `None` (the default) means
        no cloud escalation is authorized; a local worker needs none
        regardless."""
        if not isinstance(original_prompt, str):
            raise TypeError("original_prompt must be a str")
        # H.3: an ARCHIVED worker never gets a new production run at
        # all -- checked before any row is created, mirroring how an
        # unknown run_id in `resume()` returns a result with nothing
        # durable behind it. Unknown `worker_id` is unchanged: this
        # method has never validated worker existence itself (that is
        # `api.service.ApplicationService.start()`'s own existing
        # `unknown_worker` check, before it ever calls here) and still
        # does not -- only a REGISTERED, ARCHIVED worker is refused.
        existing_worker = WorkersRepo(self._control_conn).get(worker_id)
        if existing_worker is not None and existing_worker.lifecycle_state != "ACTIVE":
            return RunResult(run_id="", status=RunStatus.FAILED, reason="worker_archived")
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

        run = RunnerRepo(self._control_conn).get(run_id)
        try:
            analysis = prompt_analyst.analyze(original_prompt, {})
        except PromptAnalystError as exc:
            return self._finish(run, RunStatus.FAILED, f"prompt_analysis_failed:{exc}")
        return self._evaluate_gate(
            run, original_prompt, analysis, resolutions, adapter, cloud_escalation,
        )

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
        cloud_escalation: CloudEscalationAuthorization | None = None,
    ) -> RunResult:
        """Derive the next safe action entirely from durable state —
        never from Python object memory. Idempotent for a run already in
        a terminal status: returns it unchanged, invoking no worker and
        no tool. Concurrency-safe: claims the `READY -> RUNNING`
        transition inside one `BEGIN IMMEDIATE` transaction, so two
        concurrent `resume()` calls can never both execute the same
        bounded turn (see `_claim_for_execution()`).

        `cloud_escalation` (Phase 7.7e) must be supplied fresh on every
        call that could reach execution for a cloud worker — no prior
        authorization is durably remembered across a crash/restart; see
        `workers.cloud_escalation`'s module docstring for why that is
        deliberate.

        H.3: an ARCHIVED worker never reaches worker/model execution
        through `resume()`, on ANY durable status a run can be found
        in. A terminal run is always returned unchanged regardless of
        lifecycle (see the check above). For every non-terminal status,
        the gate below is placed exactly where that status's own
        handling would otherwise invoke the worker (`adapter is not
        None` is, precisely and only, the caller's own signal that
        this call might reach execution — `_evaluate_gate()`/
        `_proceed_to_execution()`/`_recover_mid_turn()` are all
        themselves no-ops for inference purposes when `adapter is
        None`) — never a single blanket check applied uniformly before
        branching, so `ANALYZING` (which never touches `adapter` at
        all) is untouched by this gate. Refusing here never mutates the
        run: no status/reason is written, so a legitimate later
        `resume()` after reactivation can still continue exactly where
        this one left off."""
        run = RunnerRepo(self._control_conn).get_or_none(run_id)
        if run is None:
            return RunResult(run_id=run_id, status=RunStatus.FAILED, reason="unknown_run")

        if run.status in _TERMINAL_STATUSES_NO_NEW_WORK:
            return self._to_result(run)

        if run.status == RunStatus.BLOCKED_ON_QUESTIONS.value:
            if adapter is not None and self._worker_archived(run.worker_id):
                return self._to_result(run, reason_override="worker_archived")
            original_prompt = read_original_prompt(
                self._control_conn, self._control_blobs_dir, run.original_prompt_hash,
            )
            analysis = read_prompt_analysis(
                self._control_conn, self._control_blobs_dir, run.analysis_content_hash,
            )
            self._audit(run_id, EventType.RUN_RESUMED, {"from_status": run.status})
            return self._evaluate_gate(
                run, original_prompt, analysis, resolutions, adapter, cloud_escalation,
            )

        if run.status == RunStatus.READY.value:
            if adapter is not None and self._worker_archived(run.worker_id):
                return self._to_result(run, reason_override="worker_archived")
            claimed = self._claim_for_execution(run_id)
            if claimed is None:
                return self._to_result(RunnerRepo(self._control_conn).get(run_id))
            self._audit(run_id, EventType.RUN_RESUMED, {"from_status": run.status})
            return self._proceed_to_execution(claimed, adapter, cloud_escalation)

        if run.status == RunStatus.RUNNING.value:
            if adapter is not None and self._worker_archived(run.worker_id):
                return self._to_result(run, reason_override="worker_archived")
            self._audit(run_id, EventType.RUN_RESUMED, {"from_status": run.status})
            return self._recover_mid_turn(run, adapter, cloud_escalation)

        if run.status == RunStatus.ANALYZING.value:
            # `start()` now catches every ordinary `PromptAnalystError`
            # itself and terminalizes to FAILED before ever returning --
            # a run can only still be found here if the process crashed
            # between durably creating this row and that call completing
            # (before either outcome, success or failure, could be
            # recorded). Nothing durable or irreversible happened during
            # that window (no task, no lease, no tool call --
            # `PromptAnalyst.analyze()` has no filesystem/tool access at
            # all), but `resume()` deliberately never re-invokes the
            # analyst (see the `BLOCKED_ON_QUESTIONS` branch above) and
            # takes no `prompt_analyst` argument to do so safely even if
            # it wanted to. Fails closed to INTERRUPTED_RESUMABLE --
            # the exact same "requires explicit reconciliation, never
            # guessed forward" contract already used for every other
            # crash-shaped gap in this state machine (see the module
            # docstring's "Known limitations") -- rather than leaving
            # the row lying about still being an active analysis.
            self._audit(run_id, EventType.RUN_RESUMED, {"from_status": run.status})
            return self._finish(
                run, RunStatus.INTERRUPTED_RESUMABLE,
                "analysis_interrupted_requires_reconciliation",
            )

        # INTERRUPTED_RESUMABLE: requires explicit reconciliation this
        # phase does not automate (see the module docstring's "Known
        # limitations") -- returned unchanged, never guessed forward.
        return self._to_result(run)

    # -- gate evaluation --------------------------------------------------

    def _evaluate_gate(
        self, run: RunnerRun, original_prompt: str, analysis: PromptAnalysis,
        resolutions: tuple[ResolutionEvidence, ...], adapter: WorkerAdapter | None,
        cloud_escalation: CloudEscalationAuthorization | None = None,
    ) -> RunResult:
        """Run (or re-run, on resume) `QuestionGate.evaluate()` against
        `analysis` — freshly produced by `PromptAnalyst.analyze()` when
        called from `start()`, or reconstructed from durable evidence via
        `workers.prompt_provenance.read_prompt_analysis()` when called
        from `resume()` on a `BLOCKED_ON_QUESTIONS` run (the analyst is
        never re-invoked merely to continue). `resolutions` is combined
        with every durably recorded human resolution for this run — see
        `_load_durable_human_resolutions()`."""
        human_records = self._load_durable_human_resolutions(run.run_id)
        all_resolutions = tuple(resolutions) + self._resolutions_as_evidence(human_records)
        gate_result = QuestionGate().evaluate(
            original_prompt=original_prompt, analysis=analysis, resolutions=all_resolutions,
        )
        provenance = record_prompt_analysis(
            self._control_conn, self._control_blobs_dir, task_id=run.task_id,
            analysis=analysis, gate_result=gate_result, run_id=run.run_id,
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
        return self._proceed_to_execution(claimed, adapter, cloud_escalation)

    def _load_durable_human_resolutions(self, run_id: str) -> tuple[_HumanResolution, ...]:
        """Every distinct ambiguity this run has a durable human/
        application resolution for — the single durable read both
        `_evaluate_gate()` (via `_resolutions_as_evidence()`) and
        `_execute_owned()` (via `_build_supplemental_resolutions()`) build
        on, never two independent reconstructions of the same rows.
        `RunnerRepo.latest_human_resolutions()` already resolves a
        revised answer to exactly its most recent row per `ambiguity_id`
        — this module invents no separate "latest wins" rule; it reuses
        the one the durable schema already defines. Ordered by
        `ambiguity_id` (that repo method's own `ORDER BY`), not
        insertion order — callers that need this run's ambiguity order
        instead use `PromptAnalysis.ambiguities`, never this tuple's own
        order (see `_build_supplemental_resolutions()`)."""
        rows = RunnerRepo(self._control_conn).latest_human_resolutions(run_id)
        return tuple(
            _HumanResolution(
                ambiguity_id=row.ambiguity_id, source=EvidenceSource(row.source),
                resolution_kind=ResolutionKind(row.resolution_kind),
                answer_content_hash=row.answer_content_hash,
            )
            for row in rows
        )

    @staticmethod
    def _resolutions_as_evidence(
        records: tuple[_HumanResolution, ...],
    ) -> tuple[ResolutionEvidence, ...]:
        """The `QuestionGate`-facing view of durable human resolutions —
        authority metadata only (source/kind/which ambiguity), never the
        answer text itself; `QuestionGate.evaluate()` needs nothing more
        to decide SUPPRESS/ASK. See `_build_supplemental_resolutions()`
        for the worker-facing view that *does* carry the verified answer."""
        return tuple(
            ResolutionEvidence(
                key=f"human:{record.ambiguity_id}", source=record.source,
                resolution_kind=record.resolution_kind,
                resolves_ambiguity_ids=(record.ambiguity_id,),
            )
            for record in records
        )

    def _read_verified_human_answer(self, content_hash: str) -> str:
        """Read back one durable human-resolution answer and verify its
        content identity before ever letting it become worker-facing
        context — the same fail-closed posture `workers.prompt_provenance.
        read_original_prompt()` already uses for the original prompt, and
        `workers.execution.evidence_content()` uses for tool-read
        evidence. Raises (never returns an unverified fallback, never
        silently substitutes the caller's own copy of anything) if the
        blob is missing, misclassified, or its bytes do not hash to
        `content_hash` — this must never be reached once a `WorkerRequest`
        has already been built, only strictly before."""
        store = ContentStore(self._control_conn, self._control_blobs_dir)
        meta = store.get_meta(content_hash)
        if meta is None or meta.source_kind != HUMAN_ANSWER_EVIDENCE_KIND:
            raise RuntimeError(
                f"no durable human-resolution answer evidence for {content_hash!r}"
            )
        try:
            content = store.read(content_hash)
        except OSError as exc:
            raise RuntimeError(
                f"human-resolution answer blob unreadable for {content_hash!r}"
            ) from exc
        if files.digest(content) != content_hash:
            raise RuntimeError("human-resolution answer blob content hash mismatch")
        return content.decode("utf-8")

    def _build_supplemental_resolutions(
        self, original_prompt: str, analysis: PromptAnalysis,
        human_records: tuple[_HumanResolution, ...],
    ) -> tuple[WorkerSupplementalResolution, ...]:
        """The worker-facing view of durable human resolutions (Phase
        7.7d): for each ambiguity in `analysis.ambiguities` — in that
        exact, deterministic order, never storage/insertion order —
        that a durable human resolution *by itself* (never combined with
        any ephemeral, non-durable `resolutions` argument some caller
        might also have supplied) suffices to resolve, verify and carry
        forward its exact durable answer text.

        Re-running `QuestionGate.evaluate()` here, against only the
        durable evidence, is what makes this exact and safe rather than a
        guess: `QuestionGateResult.evidence_refs` lines up positionally
        with `analysis.ambiguities` **only** when the decision is
        `SUPPRESS` (every ambiguity resolved) — which is also exactly
        the one case where forwarding is meaningful at all. If durable
        evidence alone would not suppress every ambiguity (some other,
        non-durable resolution is what actually let this run proceed),
        nothing is forwarded — this never guesses which subset the
        durable evidence alone would have covered. A resolution's
        `evidence_ref` is checked to actually start with this exact
        ambiguity's own `human:<ambiguity_id>` key before it is trusted
        for that ambiguity — the same binding discipline that stops one
        ambiguity's resolution from ever being read as another's answer.
        """
        if not human_records or not analysis.ambiguities:
            return ()
        durable_evidence = self._resolutions_as_evidence(human_records)
        durable_gate_result = QuestionGate().evaluate(
            original_prompt=original_prompt, analysis=analysis, resolutions=durable_evidence,
        )
        if durable_gate_result.decision != GateDecision.SUPPRESS:
            return ()
        records_by_ambiguity_id = {record.ambiguity_id: record for record in human_records}
        supplemental: list[WorkerSupplementalResolution] = []
        for ambiguity, evidence_ref in zip(
            analysis.ambiguities, durable_gate_result.evidence_refs, strict=True,
        ):
            record = records_by_ambiguity_id.get(ambiguity.id)
            if record is None:
                continue
            expected_prefix = f"{record.source.value.lower()}:human:{ambiguity.id}:"
            if not evidence_ref.startswith(expected_prefix):
                # This ambiguity's evidence_ref names a different key --
                # some other resolution actually resolved it, not this
                # (possibly stale, wrong-kind) durable human record.
                continue
            content = self._read_verified_human_answer(record.answer_content_hash)
            supplemental.append(WorkerSupplementalResolution(
                ambiguity_id=ambiguity.id,
                kind=WorkerSupplementalKind(record.resolution_kind.value),
                source=WorkerSupplementalSource(record.source.value),
                content=content, content_hash=record.answer_content_hash,
            ))
        return tuple(supplemental)

    def _supplemental_resolutions_for_run(
        self, run: RunnerRun, original_prompt: str,
    ) -> tuple[WorkerSupplementalResolution, ...]:
        """Reconstruct this run's worker-facing supplemental context
        entirely from durable state (Phase 7.7d) — no in-memory shortcut:
        this is called from `_execute_owned()`, reachable both directly
        after `_evaluate_gate()`'s own SUPPRESS decision and, unchanged,
        after a full process restart via `resume()`'s `RUNNING` recovery
        path. `run.analysis_content_hash` is set by `_evaluate_gate()`
        before a run ever reaches `READY`; `None` here (an execution path
        reached with no analysis on record at all) yields no supplemental
        context rather than guessing."""
        if run.analysis_content_hash is None:
            return ()
        analysis = read_prompt_analysis(
            self._control_conn, self._control_blobs_dir, run.analysis_content_hash,
        )
        human_records = self._load_durable_human_resolutions(run.run_id)
        return self._build_supplemental_resolutions(original_prompt, analysis, human_records)

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

    def _proceed_to_execution(
        self, run: RunnerRun, adapter: WorkerAdapter | None,
        cloud_escalation: CloudEscalationAuthorization | None = None,
    ) -> RunResult:
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
                run, execution, lease_result.handle, adapter, original_prompt, cloud_escalation,
            )
        finally:
            execution.close()

    def _execute_owned(
        self, run, execution, lease, adapter, original_prompt, cloud_escalation=None,
    ) -> RunResult:
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
        supplemental_resolutions = self._supplemental_resolutions_for_run(run, original_prompt)
        outcome = execute_guarded_turn(
            execution.conn, adapter, task_id=execution.task.task_id, worker_id=run.worker_id,
            role=run.role, original_prompt=original_prompt, lease=lease,
            blobs_dir=execution.blobs_dir, trust_conn=self._control_conn,
            supplemental_resolutions=supplemental_resolutions, cloud_escalation=cloud_escalation,
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

        Deterministic Finalization: a mutating job (`run.requires_mutation`,
        and not `bounded_read_only_turn`) whose worker turn completed
        without error does NOT go straight to `COMPLETED` here -- it goes
        to `TaskState.VERIFYING` instead, and `_run_finalizer_best_effort`
        drives it the rest of the way from real, executed verification
        evidence once this transaction has committed. `outcome.ok` (via
        `status == RunStatus.COMPLETED`) still decides whether the
        worker's own turn executed without error; it never again, by
        itself, decides that the *job* is done. The `bounded_read_only_
        turn` shortcut (no repository changes to verify) is unchanged.
        """
        manager = LeaseManager(execution.conn)
        verifying = False
        result: RunResult | None = None
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
                execution_kind = json.loads(task.config_json).get("execution_kind")
                verifying = (
                    status == RunStatus.COMPLETED and run.requires_mutation
                    and execution_kind != "bounded_read_only_turn"
                )
                target = (
                    TaskState.VERIFYING if verifying else
                    TaskState.COMPLETED if status == RunStatus.COMPLETED else
                    TaskState.FAILED
                )
                TaskStateMachine(
                    execution.conn, guards=(checkpointed_completion_guard(execution.conn),),
                ).transition_in_transaction(
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
                result = self._finish(run, status, reason, **kwargs)
        if result is None:
            result = self._finish(run, status, reason, **kwargs)
        if verifying:
            # Outside every transaction opened above: verification runs
            # real subprocesses and must never do so under an open write
            # lock (established discipline throughout this codebase).
            self._run_finalizer_best_effort(execution, run.task_id)
        return result

    def _run_finalizer_best_effort(self, execution, task_id: str) -> None:
        """Drive a newly-`VERIFYING` task the rest of the way -- real,
        executed verification evidence (`finalization.service.Finalizer`)
        and, when that reaches `READY_FOR_CHECKPOINT`, automatic
        checkpoint creation and guarded automatic completion
        (`finalization.lifecycle.advance_ready_for_checkpoint`/
        `advance_checkpointed_completion` -- both thin wrappers around
        the existing `CheckpointManager`/`checkpointed_completion_guard`,
        never a second implementation of either) -- under one
        freshly-acquired lease held for the whole pipeline, never the
        worker turn's just-released one, matching every other
        lease-consuming module's own acquire-per-activity discipline.
        Never called for a `bounded_read_only_turn` job at all --
        `_finish_owned()` only invokes this when `verifying` was true,
        which that shortcut never sets.

        Never lets an exception escape (this call happens after the
        worker turn's own `RunResult` was already finalized -- an
        exception here must never make that legitimate result
        unreachable): `FinalizationError` (expected fencing/state races)
        is silently benign and leaves the task at `VERIFYING` for a later
        attempt; any other exception from the verification stage is
        contained by `_record_finalizer_failure`, which durably records
        it (never a silent crash leaving the task stuck with no trace)
        using the existing `BLOCKED` state with `resume_target=VERIFYING`
        -- no new TaskState. The checkpoint/completion stages are
        exception-safe by construction (see `finalization.lifecycle`'s
        own module docstring) and never raise at all -- only while this
        call's own lease is still current (a lease that went stale gets
        no further writes from us at all, at any stage)."""
        acquired = LeaseManager(execution.conn).acquire(
            worktree_id=execution.task.worktree_id, task_id=task_id,
            worker_id="code-slayer-finalizer", worker_session_id=str(uuid.uuid4()),
        )
        if acquired.decision != Decision.ALLOW or acquired.handle is None:
            return
        handle = acquired.handle
        tmp_dir = location.tmp_dir(
            self._primary.repo_id, execution.task.worktree_id,
            override=self._state_root_override,
        )
        try:
            decision = Finalizer(
                execution.conn, blobs_dir=execution.blobs_dir, tmp_dir=tmp_dir,
            ).decide_after_verification(task_id, handle)
        except FinalizationError:
            return
        except Exception as exc:  # noqa: BLE001 -- contained and durably recorded, never re-raised
            self._record_finalizer_failure(execution, task_id, handle, exc)
        else:
            if decision.target_state == TaskState.READY_FOR_CHECKPOINT:
                checkpoint_result = advance_ready_for_checkpoint(
                    execution.conn, task_id, handle,
                    blobs_dir=execution.blobs_dir, tmp_dir=tmp_dir,
                )
                if checkpoint_result.outcome == CheckpointAdvanceOutcome.CREATED:
                    advance_checkpointed_completion(execution.conn, task_id, handle)
        finally:
            LeaseManager(execution.conn).release(handle)

    def _record_finalizer_failure(self, execution, task_id: str, lease, exc: Exception) -> None:
        """An unexpected (non-`FinalizationError`) exception escaped the
        finalizer. Fail closed: never let it propagate, and never leave
        the task silently stuck at `VERIFYING` with no durable trace.
        Reuses the existing `BLOCKED` state with the same
        `resume_target=VERIFYING` mechanics `finalization.policy`'s own
        `BLOCKED`/`INVALID_ENVIRONMENT` verdicts already rely on -- no new
        TaskState. Only ever writes while `lease` is still current at the
        moment of writing (rechecked fresh, inside the transaction); a
        lease that already went stale is never used to write anything,
        matching every other module's fencing discipline. Recording the
        failure must itself never raise past this method -- if even that
        cannot be done safely, the task is simply left at `VERIFYING`,
        exactly as it would have been before this hardening."""
        reason_code = f"finalizer_exception:{type(exc).__name__}"
        try:
            with transaction(execution.conn):
                if not LeaseManager(execution.conn).is_current(lease):
                    return
                task = TaskRepo(execution.conn).get(task_id)
                if task.state != TaskState.VERIFYING.value:
                    return
                TaskStateMachine(execution.conn).transition_in_transaction(
                    task_id, request=TransitionRequest(
                        expected_state=TaskState.VERIFYING, to_state=TaskState.BLOCKED,
                        reason=f"finalizer:BLOCKED:{reason_code}",
                    ), actor_id="finalizer",
                )
                AuditWriter(execution.conn).append(
                    task_id=task_id, event_type=EventType.FINALIZATION_DECIDED,
                    actor_type="system", actor_id="finalizer",
                    payload={
                        "verdict": "BLOCKED", "reason_code": reason_code,
                        "target_state": TaskState.BLOCKED.value,
                        "exception_type": type(exc).__name__,
                    },
                )
        except Exception:  # noqa: BLE001 -- never propagate a failure while reporting a failure
            return

    def _recover_mid_turn(
        self, run: RunnerRun, adapter: WorkerAdapter | None,
        cloud_escalation: CloudEscalationAuthorization | None = None,
    ) -> RunResult:
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
                run, execution, lease_result.handle, adapter, original_prompt, cloud_escalation,
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

    def _to_result(self, run: RunnerRun, *, reason_override: str | None = None) -> RunResult:
        """`reason_override`, when given, replaces the durable `reason`
        in the returned `RunResult` WITHOUT writing it back to `run` --
        used only for a H.3 archived-worker refusal, which must never
        mutate the run it refused to continue (see `resume()`'s own
        docstring)."""
        questions = tuple(json.loads(run.questions_json)) if run.questions_json else ()
        final_text = None
        if run.final_text_content_hash is not None:
            final_text = ContentStore(self._control_conn, self._control_blobs_dir).read(
                run.final_text_content_hash,
            ).decode("utf-8")
        return RunResult(
            run_id=run.run_id, status=RunStatus(run.status), task_id=run.task_id,
            questions=questions,
            reason=reason_override if reason_override is not None else run.reason,
            final_text=final_text,
        )
