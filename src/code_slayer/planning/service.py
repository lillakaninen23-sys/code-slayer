"""`EngineeringPlanningService`: the one public entry point for durable
engineering planning (Phase 8.2).

## PLANNING ONLY — structurally, not just by convention

This module imports no `tools.executor.ToolExecutor`, no `policy.engine.
PolicyEngine`, no `lease.manager.LeaseManager`, no `repo.checkpoint`, and
no `workers.trust.WorkerTrustManager` promotion path. A plan this module
produces cannot mutate a repository file, execute a discovered command,
acquire a lease, create a checkpoint, merge/promote anything, or grant
AUTO trust — not because some later check forbids it, but because
nothing in this module's import graph is even capable of it. `READY`
means a plan is internally consistent and evidence-backed; it never
means execution is authorized. A future execution phase, if one exists,
is a deliberately separate decision this module makes no attempt to
anticipate.

## Composition, mirroring `runner.local_worker_runner.LocalWorkerRunner`

```
original engineering request
      |
      v
persistent plan revision record (this module, control-plane database)
      |
      v
RepositoryIntelligenceService.inspect()        (Phase 8.1/8.1a — authoritative
      |                                          repository binding + evidence)
      v
Planner.plan()                                 (planning.planner — bounded
      |                                          context in, structured claim out)
      v
planning.evidence.validate_plan_against_intelligence()
      |          (every concrete repo claim checked against real evidence;
      |           an unsupported existing-file claim is corrected/rejected,
      |           never promoted)
      v
QuestionGate.evaluate()                        (workers.question_gate,
      |                                          reused completely unmodified)
      +-- ASK -> durable NEEDS_INPUT
      |
      +-- SUPPRESS -> durable READY
```

Every step is an *existing*, independently-tested boundary reused
unmodified: `intelligence.service.RepositoryIntelligenceService` for
repository evidence and staleness, `workers.question_gate.QuestionGate`/
`workers.prompt_analysis.Ambiguity`/`AmbiguityRiskClass` for ambiguity
and its resolution authority model, `store.content_store.ContentStore`
and `store.db.transaction()` for durability. This module only sequences
them and adds the durable planning-specific bookkeeping
(`engineering_plans`/`engineering_plan_human_resolutions`, schema v8)
needed to survive a process restart at any point.

## Staleness is computed, never durably transitioned

`PlanState` has no `STALE` member. A `READY` row whose own recorded
`(head_sha, working_tree_fingerprint)` no longer matches the
repository's real, current identity is reported as `effective_state ==
"STALE"` by every read (`get()`/`list()`/`status()`) — computed fresh
every time via `RepositoryIntelligenceService.is_identity_current()`,
never durably rewritten. See `store.migrations.
0008_engineering_planning`'s module comment for why.

## Reusing `workers.prompt_analysis.PromptAnalysis` as a `QuestionGate` adapter

`workers.question_gate.QuestionGate.evaluate()` takes a `PromptAnalysis`
only to read its `.ambiguities`/`.original_prompt_hash` — this module
never invents a second ambiguity-resolution engine. A planner's own
`workers.prompt_analysis.Ambiguity` list is wrapped in a throwaway
`PromptAnalysis(original_prompt=original_request, ambiguities=...)`
purely as the exact adapter shape `QuestionGate.evaluate()` already
expects; nothing about `PromptAnalysis`'s other fields is used or
persisted from it.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.intelligence.service import RepositoryIntelligenceService
from code_slayer.planning import provenance
from code_slayer.planning.evidence import validate_plan_against_intelligence
from code_slayer.planning.models import EngineeringPlanContent, OpenQuestion, PlanState
from code_slayer.planning.planner import Planner, PlannerOutcome, PlannerRequest, PlannerResponse
from code_slayer.repo import identity
from code_slayer.store import db as db_module
from code_slayer.store import location
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.planning_repo import PlanningRepo
from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceSource,
    PromptAnalysis,
)
from code_slayer.workers.question_gate import (
    GateDecision,
    QuestionGate,
    ResolutionEvidence,
    ResolutionKind,
)

_HUMAN_RESOLUTION_SOURCES = frozenset({
    EvidenceSource.ORIGINAL_PROMPT, EvidenceSource.DURABLE_TASK_EVIDENCE,
})

_NO_NEW_WORK_STATES = frozenset({PlanState.READY.value, PlanState.SUPERSEDED.value})


@dataclass(frozen=True)
class _HumanResolution:
    ambiguity_id: str
    source: EvidenceSource
    resolution_kind: ResolutionKind
    answer_content_hash: str


@dataclass(frozen=True)
class PlanRecord:
    """A structured result suitable for a future CLI/WebUI — never
    internal SQLite/connection details. `state` is the durable,
    explicitly transitioned `PlanState`; `effective_state` additionally
    reports `"STALE"` in place of `"READY"` when the repository binding
    no longer matches current reality (see the module docstring).
    `content` is `None` only before the first planning attempt has
    produced anything to validate at all (never reached by a caller of
    `create()`/`resume()`/`replan()`, which always return a
    post-attempt record)."""

    plan_id: str
    revision: int
    predecessor_plan_id: str | None
    state: str
    effective_state: str
    reason: str | None
    created_at: str
    updated_at: str
    repo_id: str
    worktree_id: str
    run_id: str | None
    head_sha: str | None
    working_tree_dirty: bool
    working_tree_fingerprint: str | None
    intelligence_snapshot_id: str | None
    questions: tuple[dict, ...]
    content: EngineeringPlanContent | None


class EngineeringPlanningService:
    """One persistent service bound to exactly one primary repository.
    Construct fresh (a new instance, new connections) after any process
    restart — nothing about resuming a plan relies on Python object
    memory from a prior instance."""

    SCHEMA_VERSION = "phase8.2-v1"

    def __init__(
        self, primary_repo_path: Path | str, *, state_root_override: str | Path | None = None,
    ) -> None:
        self._primary = identity.resolve(primary_repo_path)
        self._state_root_override = state_root_override
        self._db_path = location.db_path(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        self._blobs_dir = location.blobs_dir(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        location.ensure_dirs(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        self._conn = db_module.connect(self._db_path)
        db_module.migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _intelligence(self):
        service = RepositoryIntelligenceService(
            self._primary.repo_root, state_root_override=self._state_root_override,
        )
        try:
            yield service
        finally:
            service.close()

    def _audit(self, plan_id: str, event_type: EventType, payload: dict) -> None:
        AuditWriter(self._conn).append(
            task_id=None, event_type=event_type, actor_type="system",
            actor_id="engineering-planning-service", payload={"plan_id": plan_id, **payload},
        )

    # -- public API -------------------------------------------------------

    def create(
        self, *, original_request: str, planner: Planner, run_id: str | None = None,
        resolutions: tuple[ResolutionEvidence, ...] = (),
    ) -> PlanRecord:
        """Create a new, revision-1 plan and carry it as far as it can
        safely go in this same call: through Repository Intelligence
        binding, the planner turn, evidence validation, and Question
        Gate evaluation, to `DRAFT` (malformed output or an evidence
        defect), `NEEDS_INPUT`, or `READY`."""
        if not isinstance(original_request, str) or not original_request.strip():
            raise TypeError("original_request must be a non-empty str")
        plan_id = uuid.uuid4().hex
        now = utcnow_iso()
        store = ContentStore(self._conn, self._blobs_dir)
        with transaction(self._conn):
            request_blob = provenance.store_request(store, original_request)
            PlanningRepo(self._conn).create_in_transaction(
                plan_id=plan_id, created_at=now, schema_version=self.SCHEMA_VERSION,
                repo_id=self._primary.repo_id, worktree_id=self._primary.worktree_id,
                run_id=run_id, request_content_hash=request_blob.content_hash,
                predecessor_plan_id=None, revision=1, state=PlanState.DRAFT.value,
            )
            self._audit(plan_id, EventType.PLAN_STARTED, {
                "request_content_hash": request_blob.content_hash, "run_id": run_id,
            })
        return self._run_planning_attempt(plan_id, original_request, planner, resolutions)

    def resume(
        self, plan_id: str, *, resolutions: tuple[ResolutionEvidence, ...] = (),
    ) -> PlanRecord:
        """Re-evaluate a `NEEDS_INPUT` plan against newly available
        durable human resolutions (plus any ephemeral `resolutions`
        supplied this call) — never re-invokes the planner; its
        structured output is durable state, exactly like `runner.
        local_worker_runner.LocalWorkerRunner.resume()` never re-invokes
        the Prompt Analyst merely to continue. Idempotent for a plan
        already `READY`/`SUPERSEDED`: returns it unchanged."""
        row = PlanningRepo(self._conn).get(plan_id)
        if row.state in _NO_NEW_WORK_STATES or row.state == PlanState.DRAFT.value:
            # DRAFT here means the planning attempt itself never reached
            # evidence-validated content (malformed output or a blocking
            # defect) -- resuming cannot fix that without a fresh
            # planner call; only replan() can.
            return self.get(plan_id)
        original_request = provenance.read_request(
            self._conn, self._blobs_dir, row.request_content_hash,
        )
        content = provenance.read_plan_content(self._conn, self._blobs_dir, row.plan_content_hash)
        self._audit(plan_id, EventType.PLAN_RESUMED, {"from_state": row.state})
        return self._evaluate_gate_and_finish(plan_id, original_request, content, resolutions)

    def replan(
        self, plan_id: str, *, planner: Planner, resolutions: tuple[ResolutionEvidence, ...] = (),
    ) -> PlanRecord:
        """Create a new revision over the *same* original request,
        referencing `plan_id` as its predecessor, and mark `plan_id`
        itself `SUPERSEDED` — never rewrites or deletes its content.
        Always re-invokes the planner (unlike `resume()`): a stuck
        `DRAFT` (malformed output or an evidence defect) or an outdated
        `READY`/`NEEDS_INPUT` plan can only be corrected by a fresh
        attempt, never a silent auto-repair of the old one."""
        old = PlanningRepo(self._conn).get(plan_id)
        if old.state == PlanState.SUPERSEDED.value:
            raise ValueError(f"plan {plan_id!r} is already superseded")
        original_request = provenance.read_request(
            self._conn, self._blobs_dir, old.request_content_hash,
        )
        new_plan_id = uuid.uuid4().hex
        now = utcnow_iso()
        with transaction(self._conn):
            PlanningRepo(self._conn).create_in_transaction(
                plan_id=new_plan_id, created_at=now, schema_version=self.SCHEMA_VERSION,
                repo_id=old.repo_id, worktree_id=old.worktree_id, run_id=old.run_id,
                request_content_hash=old.request_content_hash, predecessor_plan_id=old.plan_id,
                revision=old.revision + 1, state=PlanState.DRAFT.value,
            )
            PlanningRepo(self._conn).update_in_transaction(
                old.plan_id, updated_at=now, state=PlanState.SUPERSEDED.value,
                reason="superseded_by_replan",
            )
            self._audit(old.plan_id, EventType.PLAN_SUPERSEDED, {"successor_plan_id": new_plan_id})
        return self._run_planning_attempt(new_plan_id, original_request, planner, resolutions)

    def record_user_resolution(
        self, plan_id: str, ambiguity_id: str, answer: str, *,
        resolution_kind: ResolutionKind,
        source: EvidenceSource = EvidenceSource.DURABLE_TASK_EVIDENCE,
    ) -> None:
        """The application API for answering one blocked planning
        ambiguity — mirrors `runner.local_worker_runner.
        LocalWorkerRunner.record_user_resolution()` exactly: durable,
        content-addressed, audited, and constructs trusted
        `ResolutionEvidence` bound to exactly `ambiguity_id`. Grants no
        trust and authorizes no execution."""
        if source not in _HUMAN_RESOLUTION_SOURCES:
            raise ValueError(
                "a human resolution's source must be ORIGINAL_PROMPT or DURABLE_TASK_EVIDENCE"
            )
        if not isinstance(resolution_kind, ResolutionKind):
            raise TypeError("resolution_kind must be a ResolutionKind")
        PlanningRepo(self._conn).get(plan_id)  # KeyError if the plan does not exist
        now = utcnow_iso()
        store = ContentStore(self._conn, self._blobs_dir)
        with transaction(self._conn):
            blob = store.put(
                answer.encode("utf-8"), media_type="text/plain",
                source_kind=provenance.PLAN_HUMAN_ANSWER_EVIDENCE_KIND, exportable=False,
            )
            PlanningRepo(self._conn).record_human_resolution_in_transaction(
                plan_id=plan_id, ambiguity_id=ambiguity_id, source=source.value,
                resolution_kind=resolution_kind.value, answer_content_hash=blob.content_hash,
                created_at=now,
            )
            self._audit(plan_id, EventType.PLAN_USER_RESOLUTION_RECORDED, {
                "ambiguity_id": ambiguity_id, "resolution_kind": resolution_kind.value,
                "source": source.value,
            })

    def status(self, plan_id: str) -> PlanRecord:
        """Read-only alias for `get()` at this phase's granularity —
        kept as a distinct method name for symmetry with `intelligence.
        service.RepositoryIntelligenceService.status()`/`runner.
        local_worker_runner.LocalWorkerRunner.status()`. Never mutates
        anything, never invokes a planner."""
        return self.get(plan_id)

    def get(self, plan_id: str) -> PlanRecord:
        row = PlanningRepo(self._conn).get(plan_id)
        content = (
            provenance.read_plan_content(self._conn, self._blobs_dir, row.plan_content_hash)
            if row.plan_content_hash else None
        )
        return PlanRecord(
            plan_id=row.plan_id, revision=row.revision,
            predecessor_plan_id=row.predecessor_plan_id, state=row.state,
            effective_state=self._effective_state(row), reason=row.reason,
            created_at=row.created_at, updated_at=row.updated_at, repo_id=row.repo_id,
            worktree_id=row.worktree_id, run_id=row.run_id, head_sha=row.head_sha,
            working_tree_dirty=row.working_tree_dirty,
            working_tree_fingerprint=row.working_tree_fingerprint,
            intelligence_snapshot_id=row.intelligence_snapshot_id,
            questions=self._questions_view(row, content), content=content,
        )

    def list(self, *, limit: int = 50, offset: int = 0) -> list[PlanRecord]:
        rows = PlanningRepo(self._conn).list_for_scope(
            self._primary.repo_id, self._primary.worktree_id, limit=limit, offset=offset,
        )
        return [self.get(row.plan_id) for row in rows]

    # -- internals ----------------------------------------------------------

    def _effective_state(self, row) -> str:
        """`row.state` unchanged unless it is durably `READY` but the
        repository has since changed — see the module docstring's
        "Staleness is computed" section. Never mutates `row.state`
        itself."""
        if row.state != PlanState.READY.value:
            return row.state
        if row.head_sha is None and row.working_tree_fingerprint is None:
            return row.state
        with self._intelligence() as intel:
            current = intel.is_identity_current(row.head_sha, row.working_tree_fingerprint)
        return row.state if current else "STALE"

    def _questions_view(self, row, content: EngineeringPlanContent | None) -> tuple[dict, ...]:
        if content is None:
            return ()
        answered = {
            r.ambiguity_id for r in PlanningRepo(self._conn).latest_human_resolutions(row.plan_id)
        }
        return tuple(
            {
                "ambiguity_id": oq.ambiguity_id, "question": oq.question,
                "risk_class": oq.risk_class, "resolved": oq.resolved,
                "answer_recorded": oq.ambiguity_id in answered,
            }
            for oq in content.open_questions
        )

    def _load_durable_human_resolutions(self, plan_id: str) -> tuple[_HumanResolution, ...]:
        rows = PlanningRepo(self._conn).latest_human_resolutions(plan_id)
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
        return tuple(
            ResolutionEvidence(
                key=f"human:{record.ambiguity_id}", source=record.source,
                resolution_kind=record.resolution_kind,
                resolves_ambiguity_ids=(record.ambiguity_id,),
            )
            for record in records
        )

    def _run_planning_attempt(
        self, plan_id: str, original_request: str, planner: Planner,
        resolutions: tuple[ResolutionEvidence, ...],
    ) -> PlanRecord:
        with self._intelligence() as intel:
            snapshot = intel.inspect()
            context_pack = intel.build_context_pack(original_request)
        request = PlannerRequest(
            original_request=original_request, repo_context=snapshot.projects,
            discovered_commands=snapshot.commands, context_pack=context_pack,
        )
        store = ContentStore(self._conn, self._blobs_dir)
        planner_input_blob = provenance.store_planner_input(store, request)
        response = planner.plan(request)
        if not isinstance(response, PlannerResponse):
            response = PlannerResponse(
                PlannerOutcome.MALFORMED, error="planner_returned_non_planner_response",
            )
        planner_output_blob = provenance.store_planner_output(store, response)

        with transaction(self._conn):
            PlanningRepo(self._conn).update_in_transaction(
                plan_id, updated_at=utcnow_iso(), head_sha=snapshot.head_sha,
                working_tree_dirty=snapshot.working_tree_dirty,
                working_tree_fingerprint=snapshot.working_tree_fingerprint,
                intelligence_snapshot_id=snapshot.snapshot_id,
                planner_input_content_hash=planner_input_blob.content_hash,
                planner_output_content_hash=planner_output_blob.content_hash,
            )

        if response.outcome != PlannerOutcome.STRUCTURED or response.output is None:
            return self._finish(plan_id, PlanState.DRAFT, "malformed_planner_output")

        validation = validate_plan_against_intelligence(response.output, snapshot)
        validation_blob = provenance.store_validation_result(store, validation)
        with transaction(self._conn):
            PlanningRepo(self._conn).update_in_transaction(
                plan_id, updated_at=utcnow_iso(),
                validation_content_hash=validation_blob.content_hash,
            )

        if validation.blocking:
            content_blob = provenance.store_plan_content(store, validation.content)
            with transaction(self._conn):
                PlanningRepo(self._conn).update_in_transaction(
                    plan_id, updated_at=utcnow_iso(), plan_content_hash=content_blob.content_hash,
                )
            return self._finish(plan_id, PlanState.DRAFT, "evidence_validation_failed")

        return self._evaluate_gate_and_finish(
            plan_id, original_request, validation.content, resolutions,
        )

    def _evaluate_gate_and_finish(
        self, plan_id: str, original_request: str, content: EngineeringPlanContent,
        resolutions: tuple[ResolutionEvidence, ...],
    ) -> PlanRecord:
        human_records = self._load_durable_human_resolutions(plan_id)
        all_resolutions = tuple(resolutions) + self._resolutions_as_evidence(human_records)
        shim_ambiguities = tuple(
            Ambiguity(
                id=oq.ambiguity_id, question=oq.question, rationale="",
                risk_class=AmbiguityRiskClass(oq.risk_class),
            )
            for oq in content.open_questions
        )
        analysis = PromptAnalysis(original_prompt=original_request, ambiguities=shim_ambiguities)
        gate_result = QuestionGate().evaluate(
            original_prompt=original_request, analysis=analysis, resolutions=all_resolutions,
        )
        resolved_ids = {
            reason.rsplit(":resolved_by_trusted_evidence", 1)[0]
            for reason in gate_result.reasons if reason.endswith(":resolved_by_trusted_evidence")
        }
        final_content = replace(
            content,
            open_questions=tuple(
                OpenQuestion(
                    ambiguity_id=oq.ambiguity_id, question=oq.question, risk_class=oq.risk_class,
                    resolved=oq.ambiguity_id in resolved_ids,
                )
                for oq in content.open_questions
            ),
        )
        store = ContentStore(self._conn, self._blobs_dir)
        content_blob = provenance.store_plan_content(store, final_content)
        now = utcnow_iso()
        if gate_result.decision == GateDecision.ASK:
            with transaction(self._conn):
                PlanningRepo(self._conn).update_in_transaction(
                    plan_id, updated_at=now, state=PlanState.NEEDS_INPUT.value,
                    plan_content_hash=content_blob.content_hash,
                    questions_json=json.dumps(list(gate_result.questions)),
                    reason="blocked_on_questions",
                )
                self._audit(plan_id, EventType.PLAN_BLOCKED, {
                    "questions": list(gate_result.questions), "reasons": list(gate_result.reasons),
                })
            return self.get(plan_id)

        with transaction(self._conn):
            PlanningRepo(self._conn).update_in_transaction(
                plan_id, updated_at=now, state=PlanState.READY.value,
                plan_content_hash=content_blob.content_hash, questions_json=None, reason="ready",
            )
            self._audit(plan_id, EventType.PLAN_FINISHED, {
                "state": PlanState.READY.value, "reason": "ready",
            })
        return self.get(plan_id)

    def _finish(self, plan_id: str, state: PlanState, reason: str) -> PlanRecord:
        now = utcnow_iso()
        with transaction(self._conn):
            PlanningRepo(self._conn).update_in_transaction(
                plan_id, updated_at=now, state=state.value, reason=reason,
            )
            self._audit(plan_id, EventType.PLAN_FINISHED, {"state": state.value, "reason": reason})
        return self.get(plan_id)
