"""In-memory engineering-plan shapes (Phase 8.2).

Every dataclass here is a plain, JSON-serializable evidence record —
never a live filesystem/git handle, and never, by construction, a
model's own unvalidated claim. `EngineeringPlanContent` is the complete,
*evidence-validated* result of one planning attempt
(`planning.evidence.validate_plan_against_intelligence`); a planner's
own raw, not-yet-validated proposal is `planning.planner.
PlannerStructuredOutput` instead — a structurally different type, so
nothing downstream can confuse "what the model said" with "what actually
became part of a plan."

## Core invariant: an existing-file claim is never authoritative merely
## because the planner named it

`AffectedFile.exists_in_repository` is set only by evidence validation,
never by the planner — see the module docstring of `planning.evidence`.
A `CREATE` action against a path that does not exist in the authoritative
Repository Intelligence snapshot is a legitimate **proposal** (a new
file that does not exist yet); a `MODIFY`/`DELETE`/`INSPECT` action
against a path that does not exist is a **defect** in the plan, not a
fact — `planning.evidence` never lets such a claim reach `READY`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class PlanState(StrEnum):
    """A plan revision's own durable state — layered above, and never a
    replacement for, `core.states.TaskState` or `runner.
    local_worker_runner.RunStatus`. Deliberately does NOT include
    `STALE`: see `store.migrations.0008_engineering_planning`'s module
    comment and `planning.service.EngineeringPlanningService.
    effective_state()` for why staleness is always computed, never
    durably stored.

    `DRAFT` — created, not yet through evidence validation / the
    Question Gate, or blocked on a recorded evidence-validation defect
    that requires a fresh `replan()` (never a silent auto-repair).
    `NEEDS_INPUT` — the Question Gate raised at least one unresolved
    blocking ambiguity; identical in spirit to `runner.
    local_worker_runner.RunStatus.BLOCKED_ON_QUESTIONS`.
    `READY` — every required field is valid, every repository claim
    resolved against authoritative evidence, no blocking open question,
    and the repository binding was current *at the moment this was
    computed*. READY never itself authorizes execution — see the module
    docstring of `planning.service`.
    `SUPERSEDED` — a later revision (`replan()`) replaced this one. The
    row, and everything it references, remains readable forever; only
    its `state` changed.
    """

    DRAFT = "DRAFT"
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    SUPERSEDED = "SUPERSEDED"


class AffectedFileAction(StrEnum):
    INSPECT = "inspect"
    MODIFY = "modify"
    CREATE = "create"
    DELETE = "delete"


@dataclass(frozen=True)
class EvidenceRef:
    """One citation to authoritative Repository Intelligence evidence —
    never the planner's own assertion alone. `kind` names what was
    checked (`"file_exists"`, `"symbol_exists"`, `"command_discovered"`,
    ...); `key` is the exact path/symbol/command string verified;
    `snapshot_id` names which durable snapshot verified it, so a later
    reader can always trace a claim back to exactly what evidence
    supported it."""

    kind: str
    key: str
    snapshot_id: str


@dataclass(frozen=True)
class AffectedFile:
    """One file this plan concerns. `exists_in_repository` and
    `evidence` are set exclusively by `planning.evidence` — never
    accepted as planner input verbatim (see the module docstring)."""

    path: str
    action: AffectedFileAction
    reason: str
    exists_in_repository: bool
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class PlannedChange:
    description: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveredCommandRef:
    """One command this plan's verification step relies on —
    authoritative only when it survives `planning.evidence` matching
    against `intelligence.models.Snapshot.commands` unchanged; never
    fabricated, never executed by anything in this package."""

    command: str
    purpose: str
    evidence_source: str


@dataclass(frozen=True)
class OpenQuestion:
    """One planning ambiguity, in the exact vocabulary `workers.
    question_gate.QuestionGate` already uses — see `planning.planner`'s
    reuse of `workers.prompt_analysis.Ambiguity`/`AmbiguityRiskClass`."""

    ambiguity_id: str
    question: str
    risk_class: str
    resolved: bool


@dataclass(frozen=True)
class EngineeringPlanContent:
    """The complete, evidence-validated planning content for one plan
    revision — this is what `plan_content_hash` durably references.
    Every `affected_files`/`discovered_commands`/`evidence_refs` entry
    here has already been checked against authoritative Repository
    Intelligence evidence (or explicitly marked as a not-yet-existing
    proposal); nothing here is the planner's raw, unvalidated claim."""

    goal: str
    requirements: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    affected_files: tuple[AffectedFile, ...] = ()
    planned_changes: tuple[PlannedChange, ...] = ()
    dependencies: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    verification_steps: tuple[str, ...] = ()
    discovered_commands: tuple[DiscoveredCommandRef, ...] = ()
    authority_requirements: tuple[str, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    validation_issues: tuple[str, ...] = field(default_factory=tuple)


def plan_content_to_dict(content: EngineeringPlanContent) -> dict:
    return {"format_version": 1, **asdict(content)}


def plan_content_from_dict(data: dict) -> EngineeringPlanContent:
    if data.get("format_version") != 1:
        raise ValueError(f"unsupported engineering plan content format: {data!r}")
    return EngineeringPlanContent(
        goal=data["goal"], requirements=tuple(data["requirements"]),
        assumptions=tuple(data["assumptions"]),
        affected_files=tuple(
            AffectedFile(
                path=f["path"], action=AffectedFileAction(f["action"]), reason=f["reason"],
                exists_in_repository=f["exists_in_repository"],
                evidence=tuple(EvidenceRef(**e) for e in f["evidence"]),
            )
            for f in data["affected_files"]
        ),
        planned_changes=tuple(
            PlannedChange(description=c["description"], paths=tuple(c["paths"]))
            for c in data["planned_changes"]
        ),
        dependencies=tuple(data["dependencies"]), risks=tuple(data["risks"]),
        verification_steps=tuple(data["verification_steps"]),
        discovered_commands=tuple(
            DiscoveredCommandRef(**c) for c in data["discovered_commands"]
        ),
        authority_requirements=tuple(data["authority_requirements"]),
        open_questions=tuple(OpenQuestion(**q) for q in data["open_questions"]),
        evidence_refs=tuple(EvidenceRef(**e) for e in data["evidence_refs"]),
        validation_issues=tuple(data.get("validation_issues", ())),
    )
