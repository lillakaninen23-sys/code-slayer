"""The provider-neutral planner boundary (Phase 8.2).

Mirrors `workers.prompt_analysis`'s own "supplemental analysis is never
authoritative" posture, and `workers.protocol`'s own "provider-neutral
request/response, no filesystem/tool access" posture, applied to
planning instead of prompt analysis. No provider name (Qwen, Claude,
OpenAI, Ollama, ...) appears anywhere in this module — a concrete
`Planner` is something an adapter implements against, never something
this module names (see `planning.worker_planner.WorkerAdapterPlanner`
for the one production bridge to the existing `workers.protocol.
WorkerAdapter` transport, and `planning.fake_planner.FakePlanner` for
the deterministic, fully-offline test double).

## Bounded context only

`PlannerRequest` carries the original engineering request plus a
deliberately small, already-bounded slice of Repository Intelligence
evidence (`intelligence.models.ProjectEvidence`/`CommandCandidate`/
`ContextPack`) — never a raw filesystem handle, never "the whole
repository." A `Planner` implementation has no way to read a file, run a
command, or query the database itself; every fact it can reason about
was already selected and bounded by the caller (`planning.service.
EngineeringPlanningService`) before this call.

## The planner's own output is not repository fact

`PlannerStructuredOutput` is exactly, and only, what the model proposed
— never itself validated against real repository evidence. Every
concrete claim (`affected_files`, `evidence_refs`, `discovered_commands`)
must pass through `planning.evidence.validate_plan_against_intelligence`
before any part of it can become `planning.models.
EngineeringPlanContent`. This mirrors `workers.prompt_analysis.
PromptAnalysis` being "supplemental," never authoritative, and this
module deliberately reuses `workers.prompt_analysis.Ambiguity`/
`AmbiguityRiskClass` for `PlannerStructuredOutput.ambiguities` rather
than inventing a second ambiguity vocabulary — the exact same,
already-hardened `workers.question_gate.QuestionGate` decides SUPPRESS/
ASK over a planning ambiguity exactly as it does over a prompt-analysis
one; see `planning.service` for the integration.

## Structured output only — no free-form prose

`parse_planner_output()` is the sole way raw model output (a JSON-like
mapping, however it reached this process) becomes a
`PlannerStructuredOutput` — strict shape/type checking, field by field;
an unrecognized field, wrong type, or non-mapping/non-list shape makes
the whole thing `None` (`PlannerOutcome.MALFORMED`), never a partial or
best-effort reconstruction. This module never scans free text for
anything that looks like a plan, and never lets a model embed a tool
call or an authority request inside a plain-text field to bypass this
protocol — the exact same posture `workers.protocol_validation` already
enforces for worker tool calls (`planning.worker_planner` reuses that
validator directly for the underlying transport turn)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from code_slayer.intelligence.models import CommandCandidate, ContextPack, ProjectEvidence
from code_slayer.workers.prompt_analysis import Ambiguity, AmbiguityRiskClass
from code_slayer.workers.protocol import WorkerSupplementalResolution


@dataclass(frozen=True)
class PlannerRequest:
    """Bounded planning context — never the entire repository, never a
    live filesystem/database handle. `original_request` is the complete,
    verbatim user engineering request (never a summary/truncation, same
    discipline as `workers.protocol.WorkerRequest.original_prompt`).
    `supplemental_resolutions`, when non-empty, carries durable, already
    Question-Gate-verified answers to ambiguities raised on an earlier
    `replan()` attempt for this same plan lineage — never merged into
    `original_request` itself."""

    original_request: str
    repo_context: tuple[ProjectEvidence, ...] = ()
    discovered_commands: tuple[CommandCandidate, ...] = ()
    context_pack: ContextPack | None = None
    supplemental_resolutions: tuple[WorkerSupplementalResolution, ...] = ()


@dataclass(frozen=True)
class PlannerAffectedFileProposal:
    """The planner's own, not-yet-validated claim about one file — see
    `planning.models.AffectedFile` for the validated counterpart."""

    path: str
    action: str
    reason: str


@dataclass(frozen=True)
class PlannerCommandProposal:
    command: str
    purpose: str
    evidence_source: str


@dataclass(frozen=True)
class PlannerChangeProposal:
    description: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannerEvidenceClaim:
    """One explicit claim the planner asserts is backed by real
    repository evidence — never taken at face value; see `planning.
    evidence.validate_plan_against_intelligence`."""

    kind: str
    key: str


@dataclass(frozen=True)
class PlannerStructuredOutput:
    """Exactly, and only, what the model proposed for this planning
    turn — never itself authoritative (see the module docstring).
    `ambiguities` reuses `workers.prompt_analysis.Ambiguity` unchanged —
    the planner is exactly as advisory about ambiguity as a Prompt
    Analyst is; only an independently supplied `workers.question_gate.
    ResolutionEvidence` can ever suppress one (`planning.service`)."""

    goal: str
    requirements: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    affected_files: tuple[PlannerAffectedFileProposal, ...] = ()
    planned_changes: tuple[PlannerChangeProposal, ...] = ()
    dependencies: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    verification_steps: tuple[str, ...] = ()
    discovered_commands: tuple[PlannerCommandProposal, ...] = ()
    authority_requirements: tuple[str, ...] = ()
    evidence_claims: tuple[PlannerEvidenceClaim, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()


class PlannerOutcome(StrEnum):
    """What the underlying transport turn itself produced — mirrors
    `workers.protocol_validation.ValidationOutcome`'s own
    "structural, not semantic" judgment. `STRUCTURED` means a real,
    schema-valid `PlannerStructuredOutput` was produced; `MALFORMED`
    means it was not, for any reason (bad shape, transport failure,
    adapter-reported malformed, disallowed tool, textual protocol
    leakage) — never partially trusted."""

    STRUCTURED = "STRUCTURED"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True)
class PlannerResponse:
    """`output` is populated only when `outcome == STRUCTURED`. `raw`
    is the adapter's own raw text/params, retained only for audit/
    provenance (`planning.provenance`) — never reparsed by anything
    downstream of this module."""

    outcome: PlannerOutcome
    output: PlannerStructuredOutput | None = None
    raw: str | None = None
    error: str | None = None


class Planner(Protocol):
    """The one method a real or fake planner implements — structural
    (`typing.Protocol`), matching `workers.prompt_analysis.PromptAnalyst`/
    `workers.protocol.WorkerAdapter`'s own pattern. Given no filesystem,
    tool, or database access here; only whatever bounded `PlannerRequest`
    the caller already assembled."""

    def plan(self, request: PlannerRequest) -> PlannerResponse: ...


# -- structured output parsing: strict schema, never heuristic prose --------

def _is_str_tuple(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)


def _parse_affected_file(item: object) -> PlannerAffectedFileProposal | None:
    if not isinstance(item, Mapping):
        return None
    path, action, reason = item.get("path"), item.get("action"), item.get("reason")
    if not isinstance(path, str) or not path:
        return None
    if not isinstance(action, str) or action not in ("inspect", "modify", "create", "delete"):
        return None
    if not isinstance(reason, str) or not reason:
        return None
    return PlannerAffectedFileProposal(path=path, action=action, reason=reason)


def _parse_command(item: object) -> PlannerCommandProposal | None:
    if not isinstance(item, Mapping):
        return None
    command, purpose, source = (
        item.get("command"), item.get("purpose"), item.get("evidence_source"),
    )
    if not all(isinstance(v, str) and v for v in (command, purpose, source)):
        return None
    return PlannerCommandProposal(command=command, purpose=purpose, evidence_source=source)


def _parse_change(item: object) -> PlannerChangeProposal | None:
    if not isinstance(item, Mapping):
        return None
    description = item.get("description")
    if not isinstance(description, str) or not description:
        return None
    paths = item.get("paths", [])
    if not _is_str_tuple(paths):
        return None
    return PlannerChangeProposal(description=description, paths=tuple(paths))


def _parse_evidence_claim(item: object) -> PlannerEvidenceClaim | None:
    if not isinstance(item, Mapping):
        return None
    kind, key = item.get("kind"), item.get("key")
    if not isinstance(kind, str) or not kind or not isinstance(key, str) or not key:
        return None
    return PlannerEvidenceClaim(kind=kind, key=key)


def _parse_ambiguity(item: object) -> Ambiguity | None:
    if not isinstance(item, Mapping):
        return None
    id_, question, rationale = item.get("id"), item.get("question"), item.get("rationale")
    risk = item.get("risk_class")
    if not all(isinstance(v, str) and v for v in (id_, question, rationale)):
        return None
    if not isinstance(risk, str) or risk not in AmbiguityRiskClass.__members__:
        return None
    evidence_keys = item.get("evidence_keys", [])
    if not _is_str_tuple(evidence_keys):
        return None
    substring = item.get("resolved_by_prompt_substring")
    if substring is not None and not isinstance(substring, str):
        return None
    return Ambiguity(
        id=id_, question=question, rationale=rationale,
        risk_class=AmbiguityRiskClass(risk), evidence_keys=tuple(evidence_keys),
        resolved_by_prompt_substring=substring,
    )


def _parse_list(items: object, parser) -> tuple | None:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return None
    parsed = []
    for item in items:
        result = parser(item)
        if result is None:
            return None
        parsed.append(result)
    return tuple(parsed)


_REQUIRED_FIELDS = frozenset({"goal"})
_ALLOWED_FIELDS = frozenset({
    "goal", "requirements", "assumptions", "affected_files", "planned_changes",
    "dependencies", "risks", "verification_steps", "discovered_commands",
    "authority_requirements", "evidence_claims", "ambiguities",
})


def parse_planner_output(data: object) -> PlannerStructuredOutput | None:
    """Strict, whole-shape schema validation — `None` on anything that
    does not exactly match. Never a partial reconstruction, never a
    best-effort guess, and never an attempt to salvage a plan from free
    text (see the module docstring's "Structured output only" section).
    An unrecognized top-level field rejects the whole payload, the same
    fail-closed posture `workers.question_gate.QuestionGate` already
    uses for malformed input."""
    if not isinstance(data, Mapping):
        return None
    if not _REQUIRED_FIELDS <= set(data):
        return None
    if set(data) - _ALLOWED_FIELDS:
        return None
    goal = data["goal"]
    if not isinstance(goal, str) or not goal:
        return None
    for key in ("requirements", "assumptions", "dependencies", "risks", "verification_steps",
                "authority_requirements"):
        if key in data and not _is_str_tuple(data[key]):
            return None
    affected_files = _parse_list(data.get("affected_files", []), _parse_affected_file)
    if affected_files is None:
        return None
    planned_changes = _parse_list(data.get("planned_changes", []), _parse_change)
    if planned_changes is None:
        return None
    discovered_commands = _parse_list(data.get("discovered_commands", []), _parse_command)
    if discovered_commands is None:
        return None
    evidence_claims = _parse_list(data.get("evidence_claims", []), _parse_evidence_claim)
    if evidence_claims is None:
        return None
    ambiguities = _parse_list(data.get("ambiguities", []), _parse_ambiguity)
    if ambiguities is None:
        return None
    return PlannerStructuredOutput(
        goal=goal,
        requirements=tuple(data.get("requirements", ())),
        assumptions=tuple(data.get("assumptions", ())),
        affected_files=affected_files,
        planned_changes=planned_changes,
        dependencies=tuple(data.get("dependencies", ())),
        risks=tuple(data.get("risks", ())),
        verification_steps=tuple(data.get("verification_steps", ())),
        discovered_commands=discovered_commands,
        authority_requirements=tuple(data.get("authority_requirements", ())),
        evidence_claims=evidence_claims,
        ambiguities=ambiguities,
    )
