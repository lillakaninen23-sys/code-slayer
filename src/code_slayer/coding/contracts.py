"""Coder role contracts (Track A, slice 1 — contracts/types only).

No mutation tool wiring, no state-machine integration, no live execution
path exists yet. This module defines what a Coder turn is given
(`CoderInput`) and the three, structurally distinct output concepts a
Coder turn's result is built from — nothing here calls a model, a tool,
or `TaskStateMachine`.

## Precedent, not coupling

`planning.planner`/`planning.models` is used as the structural template
— a bounded, JSON-serializable request shape; a strict, fail-closed
`to_dict`/`from_dict` pair with an explicit `format_version`; and, most
importantly, `planning.planner.parse_planner_output()`'s own pattern for
a *model-facing* parser: a closed `_ALLOWED_FIELDS` set where any
unrecognized top-level key rejects the whole payload. Coder does **not**
reuse Planner's own request/response classes, state vocabulary
(`PlanState`), or claim types. Where Coder genuinely consumes the *same*
evidence Planner does (Repository Intelligence's `ProjectEvidence`/
`CommandCandidate`/`ContextPack`, or an already-validated
`EngineeringPlanContent`), it imports those existing, code-owned types
directly rather than redefining them.

## Three distinct output concepts, structurally separated

A single ad hoc "output" type that is both (a) parseable from model
output and (b) capable of carrying authoritative facts is a real
authority-boundary hole: nothing stops a model from simply including a
`facts`/`mutations` key in its tool-call JSON, and if any parser in this
module accepted that shape, the model would have *syntactically*
supplied authoritative evidence — even though nothing was ever
independently verified. This module closes that by construction, not by
convention, with three separate types:

1. **`CoderModelResult`** — the *only* shape a model may ever produce.
   Contains exclusively advisory fields (`completed_plan_steps`,
   `unresolved_issues`, `command_suggestions`, `repair_notes`).
   `parse_coder_model_result()` is the *sole* model-facing entry point
   in this module, and its `_ALLOWED_FIELDS` set is closed: a payload
   containing `facts`, `mutations`, `policy_decisions`,
   `verification_evidence`, `affected_paths`, or any other unrecognized
   key is rejected in full (`None`), exactly like `planning.planner.
   parse_planner_output()` already does for an unrecognized planning
   field. There is no code path anywhere that lets model-supplied JSON
   become a `CoderAuthoritativeFacts` — the type doesn't even appear in
   `parse_coder_model_result()`'s vocabulary.

2. **`CoderAuthoritativeFacts`** — code-owned only. Every field is
   populated exclusively from durable, already-typed evidence: real
   `store.models.ToolOperation` rows (`coder_mutation_record_from_tool_
   operation()`), real `finalization.types.VerificationCommandResult`
   evidence from the already-implemented Finalizer, and policy decision
   strings. `build_authoritative_facts()` is the recommended
   constructor and runtime-checks that mutation/verification evidence
   are genuinely instances of those real types, not dicts or strings —
   "requiring code-owned types where practical," concretely enforced,
   not merely documented. **There is no function anywhere in this
   module that derives a `CoderAuthoritativeFacts` from a
   `CoderModelResult`** — the only legitimate source is the durable
   stores its fields are drawn from.

3. **`CoderExecutionRecord`** — a code-owned aggregate combining a
   `model_result` and `authoritative_facts`, constructed exclusively by
   runner/orchestration code *after* a turn has actually executed. It is
   **not** a model response schema: no model-facing parser exists for
   it, and none should ever be added. `coder_execution_record_to_dict`/
   `_from_dict` exist only for trusted, internal durable-storage
   round-tripping (audit/provenance persistence of a record this module
   itself already built from real evidence) — never for parsing
   anything a model or tool call produced.

## No FINAL/COMPLETED authority

No type in this module, and no function, can express or write task
completion — that authority belongs exclusively to the Finalizer
(`finalization.service.Finalizer`/`core.state_machine.
TaskStateMachine`), which this module does not import at all.
`tests/unit/test_coder_contracts.py` verifies every claim above
structurally (parsed imports, closed field sets, `isinstance` checks),
not merely by docstring convention.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum

from code_slayer.finalization.types import VerificationCommandResult
from code_slayer.intelligence.models import (
    CommandCandidate,
    ContextFile,
    ContextPack,
    ProjectEvidence,
)
from code_slayer.planning.models import (
    EngineeringPlanContent,
    plan_content_from_dict,
    plan_content_to_dict,
)
from code_slayer.store.models import ToolOperation

FORMAT_VERSION = 1


class CoderContractError(ValueError):
    """A `CoderInput`/`CoderExecutionRecord` payload was malformed, or a
    caller tried to construct `CoderAuthoritativeFacts` from something
    that was not real, already-typed evidence — stable, never includes
    raw untrusted content, mirrors `finalization.verification.
    FinalizationError`'s own "stable reason code" posture."""


# --------------------------------------------------------------------------
# Input: what a Coder turn is given
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoderTaskIdentity:
    """Immutable identity for one Coder turn. `original_prompt` is the
    complete, verbatim original request — never a summary or paraphrase
    (the same "state remembers, nothing gets silently rewritten"
    discipline `docs/CODE_SLAYER_VISION.md`§32/§59 already requires of
    every other role that sees task intent)."""

    task_id: str
    repo_id: str
    worktree_id: str
    original_prompt: str


@dataclass(frozen=True)
class ValidatedPlanReference:
    """A reference to an already-validated (`planning.models.PlanState.
    READY`) Engineering Plan. `content_hash` binds this exact validated
    content — tamper-evident, so a caller can prove *which* validated
    plan a given Coder turn actually saw. Coder never receives a
    `DRAFT`/`NEEDS_INPUT` plan and never re-validates plan content
    itself; that authority belongs entirely to `planning.evidence`/
    `planning.service`, unchanged by this module."""

    plan_id: str
    revision: int
    content_hash: str
    content: EngineeringPlanContent


@dataclass(frozen=True)
class CoderPathScope:
    """Caller-supplied context about scope — never itself an
    authorization. The actual, load-bearing decision at tool-call time
    remains `policy.engine.PolicyEngine.evaluate()`/`tools.executor.
    ToolExecutor`, unchanged and untouched by this module; this is only
    what the Coder is told, mirroring `policy.engine.PolicyInput`'s own
    scope/protected vocabulary without importing its live decision
    machinery."""

    allowed_scope: tuple[str, ...]
    owned_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoderContextEvidence:
    """Bounded repository/context evidence — never a live filesystem
    handle, never "the whole repository." Reuses `intelligence.models`'
    existing, code-owned evidence types directly (the same substrate
    `planning.planner.PlannerRequest` already draws on) rather than
    redefining them — this is deliberate shared-evidence reuse, not
    Planner-semantics coupling: nothing about *how* Coder interprets
    this evidence is tied to how Planner does."""

    repo_context: tuple[ProjectEvidence, ...] = ()
    discovered_commands: tuple[CommandCandidate, ...] = ()
    context_pack: ContextPack | None = None


@dataclass(frozen=True)
class CoderWorktreeIdentity:
    """A plain, JSON-safe snapshot of worktree identity — deliberately
    not `repo.identity.RepoIdentity` (which carries a live `Path` and is
    meant for filesystem resolution, not a durable/transmissible
    record). `baseline_head` is `None` only for a genuinely unborn HEAD,
    exactly like `repo.inspection.RepositoryInspection.head`."""

    repo_id: str
    worktree_id: str
    repo_root: str
    baseline_head: str | None


@dataclass(frozen=True)
class CoderPermissionsSnapshot:
    """A read-only snapshot of which permission grants are currently
    active and relevant — informational context only. Coder cannot
    request, grant, or expand permissions through this or any other
    type in this module; `permissions.service.PermissionService` remains
    the sole authority (`docs/SECURITY_PRIVACY_ARCHITECTURE.md`§10:
    "Models MUST NOT grant themselves permissions")."""

    active_grant_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoderToolSchemaIdentity:
    """Which tool schema/version this turn was offered — durable
    identity, not a live tool registry handle. `allowed_tools` names
    capabilities by the same strings `tools.registry.CAPABILITIES` uses,
    without importing that live registry (this is a record of what was
    offered, not the offering mechanism itself)."""

    schema_version: str
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoderRuntimeProfileIdentity:
    """Identifies which runtime profile this Coder turn ran under —
    never asserted by the model itself, always supplied by the caller.
    Deliberately a minimal subset of the full runtime-profile identity a
    future certification/routing layer (Track B) will define — only
    what one Coder turn itself needs to be reproducible/attributable,
    not a duplicate of that not-yet-built infrastructure."""

    role: str
    model_tag: str
    runtime_version: str
    context_window_tokens: int | None = None
    output_token_budget: int | None = None


@dataclass(frozen=True)
class CoderPriorRepairEvidence:
    """Structured, code-derived feedback from a prior failed attempt on
    this same task — never the Coder's own previous self-assessment.
    `failed_verification` reuses the already-committed, already-tested
    `finalization.types.VerificationCommandResult` directly (real
    evidence from the real Finalizer) rather than depending on the
    parked/uncommitted `planning.qualification` harness — approved
    amendment #4: generalized certification/repair concepts become
    normal committed infrastructure, never a dependency on parked
    experimental code."""

    attempt_number: int
    reason_code: str
    detail: str
    failed_verification: tuple[VerificationCommandResult, ...] = ()


@dataclass(frozen=True)
class CoderInput:
    """Everything one Coder turn is given. Every field is either an
    immutable identity, a durable reference, or bounded evidence —
    nothing here is a live filesystem/database/tool handle."""

    task: CoderTaskIdentity
    validated_plan: ValidatedPlanReference
    paths: CoderPathScope
    context: CoderContextEvidence
    worktree: CoderWorktreeIdentity
    permissions: CoderPermissionsSnapshot
    tool_schema: CoderToolSchemaIdentity
    runtime_profile: CoderRuntimeProfileIdentity
    prior_repair: CoderPriorRepairEvidence | None = None


# --------------------------------------------------------------------------
# 1. CoderModelResult — the ONLY shape a model may ever produce
# --------------------------------------------------------------------------


class CoderStepClaimStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CoderPlanStepClaim:
    """The Coder's OWN claim about one plan step. `status` uses the
    generic word "completed" for *one plan step*, deliberately distinct
    from `core.states.TaskState.COMPLETED` (the whole task) — a claim
    here is never task-completion authority."""

    plan_step_description: str
    status: CoderStepClaimStatus
    note: str = ""


@dataclass(frozen=True)
class CoderUnresolvedIssue:
    """The Coder's own characterization of something it believes is
    unresolved — `severity` is the model's own advisory label, never a
    code-owned risk classification (contrast `workers.prompt_analysis.
    AmbiguityRiskClass`, which IS code-interpreted)."""

    description: str
    severity: str = "advisory"


@dataclass(frozen=True)
class CoderCommandSuggestion:
    """A suggested verification/build command — never execution
    authority. Mirrors `planning.models.DiscoveredCommandRef`'s own
    "authoritative only when independently re-derived" posture: only
    `finalization.verification.ground_verification_commands()`
    (code-owned, re-derived fresh from the actual verified tree) can
    ever make a command eligible to run — this suggestion, by itself,
    authorizes nothing."""

    command: str
    purpose: str
    rationale: str = ""


@dataclass(frozen=True)
class CoderRepairNote:
    """The Coder's own narrative about what it changed to address prior
    repair feedback — advisory only, never itself evidence that the
    repair worked; `CoderAuthoritativeFacts.verification_evidence` is
    what actually proves that, if anything does."""

    note: str


@dataclass(frozen=True)
class CoderModelResult:
    """The ONLY output shape that may come from the model. Contains
    exclusively advisory fields — see the module docstring's "Three
    distinct output concepts" section. Nothing here is trusted as fact
    by anything downstream, and this type has no field, and no related
    function, through which authoritative evidence could ever arrive."""

    completed_plan_steps: tuple[CoderPlanStepClaim, ...] = ()
    unresolved_issues: tuple[CoderUnresolvedIssue, ...] = ()
    command_suggestions: tuple[CoderCommandSuggestion, ...] = ()
    repair_notes: tuple[CoderRepairNote, ...] = ()


# --------------------------------------------------------------------------
# 2. CoderAuthoritativeFacts — code-owned only, never from model/tool output
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoderMutationRecord:
    """One durable, code-derived record of an actual mutation — sourced
    exclusively from a real `store.models.ToolOperation` row (`tools.
    executor.ToolExecutor`'s own append-only journal), never from
    anything the Coder claimed. `operation_id` is the traceable link
    back to that journal; `status` is the same `store.tool_operations_
    repo.OperationStatus` vocabulary used everywhere else in this
    codebase (`SUCCEEDED` | `FAILED` | `UNKNOWN`), never a new one. The
    recommended, and only intended, construction path is
    `coder_mutation_record_from_tool_operation()` below."""

    operation_id: str
    path: str
    tool_name: str
    status: str
    before_hash: str | None
    after_hash: str | None


def coder_mutation_record_from_tool_operation(operation: ToolOperation) -> CoderMutationRecord:
    """The recommended, and only intended, way to construct a
    `CoderMutationRecord` — directly from a real, durable `store.models.
    ToolOperation` row, never from a raw dict or anything a model
    claimed. Raises `CoderContractError` (fail closed) if `operation` is
    not genuinely a `ToolOperation` instance."""
    if not isinstance(operation, ToolOperation):
        raise CoderContractError("mutation evidence must be a real ToolOperation instance")
    return CoderMutationRecord(
        operation_id=operation.operation_id, path=operation.target_resource,
        tool_name=operation.tool_name, status=operation.status,
        before_hash=operation.before_evidence, after_hash=operation.after_evidence,
    )


@dataclass(frozen=True)
class CoderAuthoritativeFacts:
    """Code-owned only. Every field here is derived exclusively from
    durable, code-owned evidence — never from `CoderModelResult`, never
    from raw model/tool JSON. There is no model-facing parser for this
    type anywhere in this module, and none should ever be added; the
    recommended construction path is `build_authoritative_facts()`
    below, which runtime-checks that mutation/verification evidence are
    genuinely the real, already-typed evidence classes, not dicts or
    strings. Empty by construction in this slice — no mutation wiring
    exists yet to populate it."""

    mutations: tuple[CoderMutationRecord, ...] = ()
    affected_paths: tuple[str, ...] = ()
    policy_decisions: tuple[str, ...] = ()
    verification_evidence: tuple[VerificationCommandResult, ...] = ()


def build_authoritative_facts(
    *, mutations: tuple[CoderMutationRecord, ...] = (),
    affected_paths: tuple[str, ...] = (),
    policy_decisions: tuple[str, ...] = (),
    verification_evidence: tuple[VerificationCommandResult, ...] = (),
) -> CoderAuthoritativeFacts:
    """The recommended construction path for `CoderAuthoritativeFacts` —
    from already-typed, code-owned evidence only. Fails closed
    (`CoderContractError`) if `mutations`/`verification_evidence`
    contain anything that is not genuinely a `CoderMutationRecord`/
    `VerificationCommandResult` instance, or if
    `affected_paths`/`policy_decisions` contain anything that is not a
    plain string — this is "requiring code-owned types where
    practical," enforced at construction time, not merely documented."""
    if not all(isinstance(item, CoderMutationRecord) for item in mutations):
        raise CoderContractError("mutations must be real CoderMutationRecord instances")
    if not all(isinstance(item, VerificationCommandResult) for item in verification_evidence):
        raise CoderContractError(
            "verification_evidence must be real VerificationCommandResult instances",
        )
    if not all(isinstance(item, str) for item in affected_paths):
        raise CoderContractError("affected_paths must be plain strings")
    if not all(isinstance(item, str) for item in policy_decisions):
        raise CoderContractError("policy_decisions must be plain strings")
    return CoderAuthoritativeFacts(
        mutations=tuple(mutations), affected_paths=tuple(affected_paths),
        policy_decisions=tuple(policy_decisions),
        verification_evidence=tuple(verification_evidence),
    )


# --------------------------------------------------------------------------
# 3. CoderExecutionRecord — code-owned aggregate, NOT a model response schema
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoderExecutionRecord:
    """Code-owned aggregate of one Coder turn — combines `model_result`
    (advisory) and `authoritative_facts` (code-derived), constructed
    exclusively by runner/orchestration code *after* a turn has actually
    executed. This is NOT a model response schema: no model-facing
    parser exists for this type, and none should ever be added — a
    model can, at most, produce a `CoderModelResult` (via
    `parse_coder_model_result()`), never a `CoderExecutionRecord`
    directly."""

    model_result: CoderModelResult = field(default_factory=CoderModelResult)
    authoritative_facts: CoderAuthoritativeFacts = field(default_factory=CoderAuthoritativeFacts)


# --------------------------------------------------------------------------
# Serialization
#
# `coder_input_*`/`coder_model_result_*`/`coder_execution_record_*` below
# are all TRUSTED, internal, durable-storage round-tripping — the same
# posture `planning.models.plan_content_to_dict`/`_from_dict` already
# uses for already-validated content. `parse_coder_model_result()`, by
# contrast, is the one and only MODEL-FACING entry point in this module
# (mirrors `planning.planner.parse_planner_output()` exactly): it is the
# sole function anywhere here that should ever be called on raw,
# untrusted model/tool-call output, and its closed field set is what
# makes supplying `facts` (or any other authoritative-looking field)
# from a model impossible to parse, rather than merely impolite.
# --------------------------------------------------------------------------


def _project_evidence_from_dict(data: dict) -> ProjectEvidence:
    return ProjectEvidence(
        kind=data["kind"], evidence_paths=tuple(data["evidence_paths"]),
        facts=dict(data.get("facts", {})),
    )


def _command_candidate_from_dict(data: dict) -> CommandCandidate:
    return CommandCandidate(
        command=data["command"], purpose=data["purpose"],
        evidence_source=data["evidence_source"], confidence=data["confidence"],
    )


def _context_pack_to_dict(pack: ContextPack) -> dict:
    return asdict(pack)


def _context_pack_from_dict(data: dict) -> ContextPack:
    return ContextPack(
        snapshot_id=data["snapshot_id"], repo_id=data["repo_id"],
        worktree_id=data["worktree_id"], head_sha=data["head_sha"], query=data["query"],
        files=tuple(ContextFile(**f) for f in data["files"]),
        projects=tuple(_project_evidence_from_dict(p) for p in data["projects"]),
        commands=tuple(_command_candidate_from_dict(c) for c in data["commands"]),
        omitted=tuple(data["omitted"]), budget_exhausted=data["budget_exhausted"],
        stale=data["stale"],
    )


def _verification_result_to_dict(result: VerificationCommandResult) -> dict:
    return {
        "command": result.command, "purpose": result.purpose,
        "evidence_source": result.evidence_source, "confidence": result.confidence,
        "argv": list(result.argv), "operation_id": result.operation_id,
        "status": result.status, "returncode": result.returncode,
        "truncated": result.truncated, "timed_out": result.timed_out,
        "reason": result.reason,
    }


def _verification_result_from_dict(data: dict) -> VerificationCommandResult:
    return VerificationCommandResult(
        command=data["command"], purpose=data["purpose"],
        evidence_source=data["evidence_source"], confidence=data["confidence"],
        argv=tuple(data["argv"]), operation_id=data["operation_id"],
        status=data["status"], returncode=data["returncode"],
        truncated=data["truncated"], timed_out=data["timed_out"], reason=data["reason"],
    )


def coder_input_to_dict(coder_input: CoderInput) -> dict:
    if not isinstance(coder_input, CoderInput):
        raise CoderContractError("not a CoderInput")
    return {
        "format_version": FORMAT_VERSION,
        "task": asdict(coder_input.task),
        "validated_plan": {
            "plan_id": coder_input.validated_plan.plan_id,
            "revision": coder_input.validated_plan.revision,
            "content_hash": coder_input.validated_plan.content_hash,
            "content": plan_content_to_dict(coder_input.validated_plan.content),
        },
        "paths": asdict(coder_input.paths),
        "context": {
            "repo_context": [asdict(p) for p in coder_input.context.repo_context],
            "discovered_commands": [asdict(c) for c in coder_input.context.discovered_commands],
            "context_pack": (
                _context_pack_to_dict(coder_input.context.context_pack)
                if coder_input.context.context_pack is not None else None
            ),
        },
        "worktree": asdict(coder_input.worktree),
        "permissions": asdict(coder_input.permissions),
        "tool_schema": asdict(coder_input.tool_schema),
        "runtime_profile": asdict(coder_input.runtime_profile),
        "prior_repair": (
            {
                "attempt_number": coder_input.prior_repair.attempt_number,
                "reason_code": coder_input.prior_repair.reason_code,
                "detail": coder_input.prior_repair.detail,
                "failed_verification": [
                    _verification_result_to_dict(r)
                    for r in coder_input.prior_repair.failed_verification
                ],
            }
            if coder_input.prior_repair is not None else None
        ),
    }


def coder_input_from_dict(data: dict) -> CoderInput:
    if not isinstance(data, dict):
        raise CoderContractError("coder input payload must be a mapping")
    if data.get("format_version") != FORMAT_VERSION:
        raise CoderContractError(f"unsupported coder input format: {data.get('format_version')!r}")
    try:
        task_data = data["task"]
        task = CoderTaskIdentity(
            task_id=task_data["task_id"], repo_id=task_data["repo_id"],
            worktree_id=task_data["worktree_id"], original_prompt=task_data["original_prompt"],
        )
        plan_data = data["validated_plan"]
        validated_plan = ValidatedPlanReference(
            plan_id=plan_data["plan_id"], revision=plan_data["revision"],
            content_hash=plan_data["content_hash"],
            content=plan_content_from_dict(plan_data["content"]),
        )
        paths_data = data["paths"]
        paths = CoderPathScope(
            allowed_scope=tuple(paths_data["allowed_scope"]),
            owned_paths=tuple(paths_data.get("owned_paths", ())),
            protected_paths=tuple(paths_data.get("protected_paths", ())),
        )
        context_data = data["context"]
        context = CoderContextEvidence(
            repo_context=tuple(
                _project_evidence_from_dict(p) for p in context_data.get("repo_context", ())
            ),
            discovered_commands=tuple(
                _command_candidate_from_dict(c)
                for c in context_data.get("discovered_commands", ())
            ),
            context_pack=(
                _context_pack_from_dict(context_data["context_pack"])
                if context_data.get("context_pack") is not None else None
            ),
        )
        worktree_data = data["worktree"]
        worktree = CoderWorktreeIdentity(
            repo_id=worktree_data["repo_id"], worktree_id=worktree_data["worktree_id"],
            repo_root=worktree_data["repo_root"], baseline_head=worktree_data["baseline_head"],
        )
        permissions = CoderPermissionsSnapshot(
            active_grant_keys=tuple(data["permissions"].get("active_grant_keys", ())),
        )
        tool_schema_data = data["tool_schema"]
        tool_schema = CoderToolSchemaIdentity(
            schema_version=tool_schema_data["schema_version"],
            allowed_tools=tuple(tool_schema_data.get("allowed_tools", ())),
        )
        profile_data = data["runtime_profile"]
        runtime_profile = CoderRuntimeProfileIdentity(
            role=profile_data["role"], model_tag=profile_data["model_tag"],
            runtime_version=profile_data["runtime_version"],
            context_window_tokens=profile_data.get("context_window_tokens"),
            output_token_budget=profile_data.get("output_token_budget"),
        )
        prior_repair_data = data.get("prior_repair")
        prior_repair = None
        if prior_repair_data is not None:
            prior_repair = CoderPriorRepairEvidence(
                attempt_number=prior_repair_data["attempt_number"],
                reason_code=prior_repair_data["reason_code"],
                detail=prior_repair_data["detail"],
                failed_verification=tuple(
                    _verification_result_from_dict(r)
                    for r in prior_repair_data.get("failed_verification", ())
                ),
            )
    except (KeyError, TypeError) as exc:
        raise CoderContractError(f"malformed coder input payload: {exc!r}") from exc
    return CoderInput(
        task=task, validated_plan=validated_plan, paths=paths, context=context,
        worktree=worktree, permissions=permissions, tool_schema=tool_schema,
        runtime_profile=runtime_profile, prior_repair=prior_repair,
    )


def coder_model_result_to_dict(result: CoderModelResult) -> dict:
    """Trusted, internal round-tripping only — see the module's
    "Serialization" section header. Never call this to interpret raw
    model/tool output; use `parse_coder_model_result()` for that."""
    if not isinstance(result, CoderModelResult):
        raise CoderContractError("not a CoderModelResult")
    return {
        "format_version": FORMAT_VERSION,
        "completed_plan_steps": [
            {"plan_step_description": c.plan_step_description, "status": c.status.value,
             "note": c.note}
            for c in result.completed_plan_steps
        ],
        "unresolved_issues": [asdict(i) for i in result.unresolved_issues],
        "command_suggestions": [asdict(c) for c in result.command_suggestions],
        "repair_notes": [asdict(n) for n in result.repair_notes],
    }


def coder_model_result_from_dict(data: dict) -> CoderModelResult:
    """Trusted, internal round-tripping only (raises on any malformed
    input) — NOT the model-facing entry point. Use
    `parse_coder_model_result()` to interpret raw model/tool output."""
    if not isinstance(data, dict):
        raise CoderContractError("coder model result payload must be a mapping")
    if data.get("format_version") != FORMAT_VERSION:
        raise CoderContractError(
            f"unsupported coder model result format: {data.get('format_version')!r}",
        )
    try:
        return CoderModelResult(
            completed_plan_steps=tuple(
                CoderPlanStepClaim(
                    plan_step_description=c["plan_step_description"],
                    status=CoderStepClaimStatus(c["status"]), note=c.get("note", ""),
                )
                for c in data.get("completed_plan_steps", ())
            ),
            unresolved_issues=tuple(
                CoderUnresolvedIssue(**i) for i in data.get("unresolved_issues", ())
            ),
            command_suggestions=tuple(
                CoderCommandSuggestion(**c) for c in data.get("command_suggestions", ())
            ),
            repair_notes=tuple(CoderRepairNote(**n) for n in data.get("repair_notes", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CoderContractError(f"malformed coder model result payload: {exc!r}") from exc


_MODEL_RESULT_ALLOWED_FIELDS = frozenset({
    "completed_plan_steps", "unresolved_issues", "command_suggestions", "repair_notes",
})
_STEP_CLAIM_STATUSES = frozenset(s.value for s in CoderStepClaimStatus)


def _is_mapping(value: object) -> bool:
    return isinstance(value, dict)


def _parse_list(items: object, parser) -> tuple | None:
    if not isinstance(items, (list, tuple)):
        return None
    parsed = []
    for item in items:
        result = parser(item)
        if result is None:
            return None
        parsed.append(result)
    return tuple(parsed)


def _parse_step_claim(item: object) -> CoderPlanStepClaim | None:
    if not _is_mapping(item):
        return None
    if set(item) - {"plan_step_description", "status", "note"}:
        return None
    description, status = item.get("plan_step_description"), item.get("status")
    if not isinstance(description, str) or not description:
        return None
    if not isinstance(status, str) or status not in _STEP_CLAIM_STATUSES:
        return None
    note = item.get("note", "")
    if not isinstance(note, str):
        return None
    return CoderPlanStepClaim(
        plan_step_description=description, status=CoderStepClaimStatus(status), note=note,
    )


def _parse_unresolved_issue(item: object) -> CoderUnresolvedIssue | None:
    if not _is_mapping(item):
        return None
    if set(item) - {"description", "severity"}:
        return None
    description = item.get("description")
    if not isinstance(description, str) or not description:
        return None
    severity = item.get("severity", "advisory")
    if not isinstance(severity, str):
        return None
    return CoderUnresolvedIssue(description=description, severity=severity)


def _parse_command_suggestion(item: object) -> CoderCommandSuggestion | None:
    if not _is_mapping(item):
        return None
    if set(item) - {"command", "purpose", "rationale"}:
        return None
    command, purpose = item.get("command"), item.get("purpose")
    if not isinstance(command, str) or not command:
        return None
    if not isinstance(purpose, str) or not purpose:
        return None
    rationale = item.get("rationale", "")
    if not isinstance(rationale, str):
        return None
    return CoderCommandSuggestion(command=command, purpose=purpose, rationale=rationale)


def _parse_repair_note(item: object) -> CoderRepairNote | None:
    if not _is_mapping(item):
        return None
    if set(item) - {"note"}:
        return None
    note = item.get("note")
    if not isinstance(note, str) or not note:
        return None
    return CoderRepairNote(note=note)


def parse_coder_model_result(data: object) -> CoderModelResult | None:
    """The ONE model-facing parser in this module. Strict, whole-shape
    schema validation — `None` on anything that does not exactly match,
    mirroring `planning.planner.parse_planner_output()`'s exact posture
    (never a partial reconstruction, never a best-effort guess).

    `_MODEL_RESULT_ALLOWED_FIELDS` is a closed set of exactly four
    advisory field names. **A payload containing `facts`, `mutations`,
    `policy_decisions`, `verification_evidence`, `affected_paths`, or
    any other field not in that set is rejected in full** — the same
    "an unrecognized top-level field rejects the whole payload"
    fail-closed rule `parse_planner_output()` already enforces. This is
    what makes it structurally impossible for a model's tool-call JSON
    to ever become a `CoderAuthoritativeFacts`: the return type of this
    function is always `CoderModelResult | None`, never anything else,
    and `CoderAuthoritativeFacts` is not even a name this function's
    logic can reach."""
    if not isinstance(data, dict):
        return None
    if set(data) - _MODEL_RESULT_ALLOWED_FIELDS:
        return None
    completed_plan_steps = _parse_list(data.get("completed_plan_steps", []), _parse_step_claim)
    if completed_plan_steps is None:
        return None
    unresolved_issues = _parse_list(data.get("unresolved_issues", []), _parse_unresolved_issue)
    if unresolved_issues is None:
        return None
    command_suggestions = _parse_list(
        data.get("command_suggestions", []), _parse_command_suggestion,
    )
    if command_suggestions is None:
        return None
    repair_notes = _parse_list(data.get("repair_notes", []), _parse_repair_note)
    if repair_notes is None:
        return None
    return CoderModelResult(
        completed_plan_steps=completed_plan_steps, unresolved_issues=unresolved_issues,
        command_suggestions=command_suggestions, repair_notes=repair_notes,
    )


def coder_execution_record_to_dict(record: CoderExecutionRecord) -> dict:
    """Trusted, internal durable-storage round-tripping only — never a
    model-facing schema. See the module docstring's item 3."""
    if not isinstance(record, CoderExecutionRecord):
        raise CoderContractError("not a CoderExecutionRecord")
    return {
        "format_version": FORMAT_VERSION,
        "model_result": coder_model_result_to_dict(record.model_result),
        "authoritative_facts": {
            "mutations": [asdict(mut) for mut in record.authoritative_facts.mutations],
            "affected_paths": list(record.authoritative_facts.affected_paths),
            "policy_decisions": list(record.authoritative_facts.policy_decisions),
            "verification_evidence": [
                _verification_result_to_dict(r)
                for r in record.authoritative_facts.verification_evidence
            ],
        },
    }


def coder_execution_record_from_dict(data: dict) -> CoderExecutionRecord:
    """Trusted, internal durable-storage round-tripping only — reads
    back a `CoderExecutionRecord` this module itself already wrote (via
    `coder_execution_record_to_dict`); never call this on anything a
    model or tool call produced directly."""
    if not isinstance(data, dict):
        raise CoderContractError("coder execution record payload must be a mapping")
    if data.get("format_version") != FORMAT_VERSION:
        raise CoderContractError(
            f"unsupported coder execution record format: {data.get('format_version')!r}",
        )
    try:
        model_result = coder_model_result_from_dict(data["model_result"])
        facts_data = data["authoritative_facts"]
        authoritative_facts = build_authoritative_facts(
            mutations=tuple(CoderMutationRecord(**m) for m in facts_data.get("mutations", ())),
            affected_paths=tuple(facts_data.get("affected_paths", ())),
            policy_decisions=tuple(facts_data.get("policy_decisions", ())),
            verification_evidence=tuple(
                _verification_result_from_dict(r)
                for r in facts_data.get("verification_evidence", ())
            ),
        )
    except (KeyError, TypeError, ValueError, CoderContractError) as exc:
        raise CoderContractError(f"malformed coder execution record payload: {exc!r}") from exc
    return CoderExecutionRecord(model_result=model_result, authoritative_facts=authoritative_facts)
