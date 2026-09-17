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
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from code_slayer.intelligence.models import CommandCandidate, ContextPack, ProjectEvidence
from code_slayer.workers.prompt_analysis import Ambiguity, AmbiguityRiskClass
from code_slayer.workers.protocol import WorkerSupplementalResolution, WorkerUsage


@dataclass(frozen=True)
class PlannerRequest:
    """Bounded planning context — never the entire repository, never a
    live filesystem/database handle. `original_request` is the complete,
    verbatim user engineering request (never a summary/truncation, same
    discipline as `workers.protocol.WorkerRequest.original_prompt`).
    `supplemental_resolutions`, when non-empty, carries durable, already
    Question-Gate-verified answers to ambiguities raised on an earlier
    `replan()` attempt for this same plan lineage — never merged into
    `original_request` itself.

    `prior_attempt_feedback` (Phase 8.2e's qualification self-correction
    extension), when set, is a short, deterministic, code-generated
    diagnostic describing exactly why an earlier attempt at this *same*
    planning turn was rejected and what to correct — never model prose,
    never itself the answer to the underlying task. It is threaded
    through to `workers.protocol.WorkerRequest.prior_tool_result` by
    `planning.worker_planner.WorkerAdapterPlanner` unchanged — reusing
    that existing "one prior tool result" transport rather than adding a
    second one. `None` (the default) is what every ordinary planning
    turn uses today; nothing in `planning.service.
    EngineeringPlanningService` ever sets this field, so production
    planning behavior is completely unchanged by its existence. Only
    `planning.qualification`'s retry harness constructs a `PlannerRequest`
    with this field set.

    `output_token_budget` (Phase 8.2e's context-adequacy hardening), when
    set, is threaded unchanged to `workers.protocol.WorkerRequest.
    max_output_tokens` — a genuine, runtime-enforced completion-length
    cap (standard OpenAI-compatible `max_tokens`), not just bookkeeping.
    `None` (the default) omits it entirely, exactly like every ordinary
    production planning turn today; only `planning.qualification` ever
    sets this, and only when its own `RuntimeContextProfile` says
    enforcement has actually been verified for the runtime in use."""

    original_request: str
    repo_context: tuple[ProjectEvidence, ...] = ()
    discovered_commands: tuple[CommandCandidate, ...] = ()
    context_pack: ContextPack | None = None
    supplemental_resolutions: tuple[WorkerSupplementalResolution, ...] = ()
    prior_attempt_feedback: str | None = None
    output_token_budget: int | None = None


def render_bounded_context(request: PlannerRequest) -> dict:
    """The one, deterministic, JSON-safe bounded-context view of a
    `PlannerRequest` — shared by `planning.worker_planner.
    WorkerAdapterPlanner` (what actually gets sent to the model) and
    `planning.provenance.store_planner_input()` (what gets durably
    recorded), so the two can never silently drift apart (Phase 8.2b).

    Deliberately **excludes** `request.context_pack.projects`/
    `.commands` — `intelligence.query.build_context_pack()` copies
    `Snapshot.projects`/`Snapshot.commands` onto every `ContextPack` for
    its own, unrelated callers' convenience, but a planner turn already
    receives those same facts exactly once via `request.repo_context`/
    `request.discovered_commands` above; repeating them a second time
    nested inside `context_pack` would be pure duplication, the exact
    kind of unbounded-context growth that produced this phase's
    151211-byte production failure (see `planning.limits`'s module
    docstring). Also deliberately **excludes** the full `context_pack.
    omitted` path list — the low-ranked/excluded files a planner turn
    was not given carry no positive evidence value; only their count is
    retained, so a caller can still see that truncation happened without
    ever growing the prompt with names of files that were intentionally
    left out."""
    context_pack = None
    if request.context_pack is not None:
        pack = request.context_pack
        context_pack = {
            "files": [
                {
                    "path": f.path,
                    "content": f.content,
                    "truncated": f.truncated,
                    "reasons": list(f.reasons),
                }
                for f in pack.files
            ],
            "omitted_count": len(pack.omitted),
            "budget_exhausted": pack.budget_exhausted,
            "stale": pack.stale,
        }
    return {
        "repo_context": [asdict(p) for p in request.repo_context],
        "discovered_commands": [asdict(c) for c in request.discovered_commands],
        "context_pack": context_pack,
    }


class PlannerFailureCategory(StrEnum):
    """A stable, coarse, code-owned taxonomy of why one planning turn
    did not produce `PlannerOutcome.STRUCTURED` — Phase 8.2b's own
    requirement that a transport failure, a non-tool response (plain
    text, an unauthorized/wrong tool call, textual tool-protocol
    leakage), and a genuine tool call whose own parameters failed
    `parse_planner_output()`'s strict schema check must remain
    distinguishable in durable status/provenance, without ever exposing
    raw model output through that same channel (see `planning.service`'s
    use of this in the durable `reason` field, and `planning.
    provenance.store_planner_output()` for where the full, raw detail —
    `PlannerResponse.raw`/`.error` — is kept, internal-only).

    `TRANSPORT_ERROR` — the call to the model never produced a response
    at all (network/timeout/HTTP-status/malformed-JSON failure at the
    transport layer, `workers.protocol.WorkerAdapterError`).
    `NON_TOOL_RESPONSE` — a response was received, but it was not a
    valid, authorized `emit_engineering_plan` tool call: plain `TEXT`
    (exactly the observed live failure), textual tool-call-transport
    syntax leaked into text, a tool call naming something else, or an
    unauthorized/malformed tool call (`workers.protocol_validation.
    validate_response()`'s own judgment).
    `SCHEMA_INVALID` — the model *did* make a genuine, authorized
    `emit_engineering_plan` tool call, but its own `params` did not pass
    `parse_planner_output()`'s strict, whole-shape schema check.
    """

    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    NON_TOOL_RESPONSE = "NON_TOOL_RESPONSE"
    SCHEMA_INVALID = "SCHEMA_INVALID"


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


class ToolCallTransport(StrEnum):
    """How one planning turn's `WorkerToolCall` actually arrived —
    provenance of *this turn's behavior*, never a capability grant.

    `NATIVE` — the adapter itself classified the provider response as a
    genuine structured `tool_calls` payload (`WorkerResponseKind.
    TOOL_CALL`). Native transport always takes precedence over any
    compatibility decoder (`planning.worker_planner.WorkerAdapterPlanner`).
    `NORMALIZED` — native transport did not fire, the operator had
    explicitly enabled a `workers.protocol_normalization.
    ToolProtocolNormalizer` for this runtime, and that decoder accepted
    the leaked textual protocol as an exact grammar match. A normalized
    call is then run through the exact same `validate_response()` /
    `parse_planner_output()` path a native call already uses; this flag
    exists so native vs. normalized behavior remains distinguishable in
    durable provenance (`planning.provenance.store_planner_output`) and
    qualification evidence (`planning.qualification.AttemptProvenance`).
    """

    NATIVE = "NATIVE"
    NORMALIZED = "NORMALIZED"


@dataclass(frozen=True)
class PlannerResponse:
    """`output` is populated only when `outcome == STRUCTURED`. `raw`
    is the adapter's own raw text/params, retained only for audit/
    provenance (`planning.provenance`) — never reparsed by anything
    downstream of this module, and never surfaced through `planning.
    service.PlanRecord`/the HTTP API (Phase 8.2b — see `planning.
    provenance.PLANNER_OUTPUT_EVIDENCE_KIND`, internal-only durable
    storage). `failure_category`, populated whenever `outcome ==
    MALFORMED`, is the stable, coarse `PlannerFailureCategory` a caller
    may safely surface in durable status without leaking raw model
    output — see that enum's own docstring. `usage`, when the underlying
    adapter reported it (`workers.protocol.WorkerResponse.usage`), is
    real, runtime-reported token accounting for this exact turn — used
    by `planning.qualification` as evidence of what the runtime actually
    evaluated (never, by itself, proof the full untruncated request
    survived — see that module's own "expected vs. actual" section).
    `finish_reason`, when reported, distinguishes a response the model
    completed on its own (`"stop"`/`"tool_calls"`) from one cut short by
    a token cap (`"length"`) — `planning.qualification` uses this to
    classify output-budget exhaustion separately from a genuine schema/
    protocol failure, rather than trying to interpret truncated JSON as
    if it were a deliberate malformed response.

    `tool_call_transport` records whether this turn's tool call (when
    one was produced at all) arrived through genuine native transport or
    through an explicitly-enabled compatibility normalizer — see
    `ToolCallTransport`. `normalizer_id`/`normalizer_version`/
    `normalization_reason` are populated only for `NORMALIZED` turns,
    and only with the code-owned identity/reason of the decoder that
    actually ran — never raw model text.

    `original_transport_text` is populated only for `NORMALIZED` turns:
    the exact provider/model textual payload that required
    normalization, retained solely so `planning.provenance.
    store_planner_output()` can persist it as distinct, internal-only,
    content-addressed evidence. It is never the canonical structured
    output (`raw` remains that), never inspected by
    `parse_planner_output()` / `classify_planner_response()` /
    certification / permissions / trust, and never surfaced through
    `planning.service.PlanRecord` or the HTTP API."""

    outcome: PlannerOutcome
    output: PlannerStructuredOutput | None = None
    raw: str | None = None
    error: str | None = None
    failure_category: PlannerFailureCategory | None = None
    usage: WorkerUsage | None = None
    finish_reason: str | None = None
    tool_call_transport: ToolCallTransport | None = None
    normalizer_id: str | None = None
    normalizer_version: int | None = None
    normalization_reason: str | None = None
    original_transport_text: str | None = None


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
        item.get("command"),
        item.get("purpose"),
        item.get("evidence_source"),
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
        id=id_,
        question=question,
        rationale=rationale,
        risk_class=AmbiguityRiskClass(risk),
        evidence_keys=tuple(evidence_keys),
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
_ALLOWED_FIELDS = frozenset(
    {
        "goal",
        "requirements",
        "assumptions",
        "affected_files",
        "planned_changes",
        "dependencies",
        "risks",
        "verification_steps",
        "discovered_commands",
        "authority_requirements",
        "evidence_claims",
        "ambiguities",
    }
)
# Public aliases of the same field-name authority `parse_planner_output()`
# itself enforces -- `planning.worker_planner.build_default_normalizer_
# registry()` reuses these rather than duplicating the Planner schema
# inside the compatibility decoder (see `workers.qwen_textual_tool_
# normalizer.QwenTextualToolNormalizer`).
STRUCTURED_OUTPUT_FIELDS = _ALLOWED_FIELDS
STRUCTURED_OUTPUT_STRING_FIELDS = frozenset({"goal"})


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
    for key in (
        "requirements",
        "assumptions",
        "dependencies",
        "risks",
        "verification_steps",
        "authority_requirements",
    ):
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
