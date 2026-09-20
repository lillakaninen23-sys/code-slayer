"""The Planner->Coder handoff: an explicit, validated contract, never
free-form prose treated as authorization.

`validate_planner_handoff()` is the ONLY legitimate way a `coding.
contracts.ValidatedPlanReference` gets constructed in this codebase. It
never accepts a caller-supplied `EngineeringPlanContent` -- content is
always re-read, and hash-verified, from durable storage
(`planning.provenance.read_plan_content()`, the same trusted read every
other planning consumer uses) via the plan's OWN durable
`plan_content_hash`, never trusted from an argument a caller could have
fabricated or gone stale.

`validate_mutation_scope()` is the companion check that binds the
mutation scope a Coder turn will actually be granted
(`coding.pipeline.run_coding_job()`'s own `allowed_scope` parameter) to
what the validated plan itself declared. Without it, `allowed_scope`
would be a wholly independent, caller-trusted argument -- a caller could
legitimately pass `(".",)` even for a plan that only ever authorized a
single file, silently broadening what the Coder is actually authorized
to touch far past what the Planner evidence supports.

`validate_mutation_scope()` derives its authorized-path set from
`EngineeringPlanContent.affected_files` -- never `planned_changes`.
`AffectedFile.path`/`.action` are independently repository-evidence-
validated by `planning.evidence.validate_plan_against_intelligence()`
(checked against a real `intelligence.models.Snapshot`; a
`modify`/`delete` claim against a path that does not exist, or a
`create` claim against one that already does, is a blocking defect that
keeps the plan from ever reaching `READY` in the first place).
`PlannedChange.paths`, by contrast, is copied verbatim from the
Planner model's own raw proposal with NO repository-evidence check of
any kind (see `planning.evidence`'s own module docstring: "a
model-generated claim is not repository fact") -- using it here would
let the same untrusted actor this authorization boundary exists to
constrain simply declare whatever scope it wants for itself.

Fails closed with a stable `insufficient_coder_scope:<reason>` message
(`PipelineContractError`) for every insufficiency this module knows how
to name -- never a bare `False`/`None` a caller could accidentally treat
as "proceed anyway"."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from code_slayer.coding.contracts import ValidatedPlanReference
from code_slayer.coding.pipeline_types import PipelineContractError
from code_slayer.planning import provenance
from code_slayer.planning.models import AffectedFileAction, EngineeringPlanContent, PlanState
from code_slayer.store.models import EngineeringPlanRow
from code_slayer.tools import file_tools as files
from code_slayer.tools.models import ToolError

# `INSPECT` means "read for context" -- never a mutation authorization.
# Only these three actions name a path the Planner actually intends to
# change; matches `planning.models.AffectedFileAction`'s own vocabulary
# exactly, never a second, parallel classification.
_MUTATING_ACTIONS = frozenset({
    AffectedFileAction.CREATE, AffectedFileAction.MODIFY, AffectedFileAction.DELETE,
})


def validate_planner_handoff(
    conn: sqlite3.Connection, blobs_dir: Path | str, plan: EngineeringPlanRow,
) -> ValidatedPlanReference:
    """Validate that `plan` is a genuinely sufficient basis for a Coder
    turn and return the durable, tamper-evident reference to it.

    Checked, in order (first failure wins): `plan.state == READY` (never
    `DRAFT`/`NEEDS_INPUT`/`SUPERSEDED` -- `planning.evidence`/`planning.
    service` are the sole authority for what counts as READY, this
    function only reads their already-durable conclusion); a
    `plan_content_hash` actually exists (a READY plan with no recorded
    content is an internal inconsistency, never silently treated as
    "nothing to hand off"); the content is genuinely readable and
    hash-verified from durable storage; and the validated content names
    at least one planned change (a READY plan with an empty change set
    gives a Coder turn nothing to authorize)."""
    if not isinstance(plan, EngineeringPlanRow):
        raise PipelineContractError("insufficient_coder_scope:not_a_plan_row")
    if plan.state != PlanState.READY.value:
        raise PipelineContractError(f"insufficient_coder_scope:plan_not_ready:{plan.state}")
    if not plan.plan_content_hash:
        raise PipelineContractError("insufficient_coder_scope:missing_plan_content")
    try:
        content = provenance.read_plan_content(conn, blobs_dir, plan.plan_content_hash)
    except (KeyError, OSError, ValueError, RuntimeError) as exc:
        raise PipelineContractError(
            f"insufficient_coder_scope:plan_content_unverifiable:{exc}",
        ) from exc
    if not content.planned_changes:
        raise PipelineContractError("insufficient_coder_scope:no_planned_changes")
    return ValidatedPlanReference(
        plan_id=plan.plan_id, revision=plan.revision,
        content_hash=plan.plan_content_hash, content=content,
    )


def validate_mutation_scope(
    content: EngineeringPlanContent, allowed_scope: tuple[str, ...],
) -> None:
    """Require every entry in `allowed_scope` to be a path the validated
    plan's own EVIDENCE actually authorizes for mutation -- the union of
    every `AffectedFile.path` in `content.affected_files` whose `action`
    is `CREATE`/`MODIFY`/`DELETE` (never `INSPECT`, which only means
    "read for context") -- never broader. Raises `PipelineContractError`
    (fail closed, same `insufficient_coder_scope:<reason>` family
    `validate_planner_handoff()` uses) the moment `allowed_scope` claims
    anything that authorized set did not.

    Deliberately `affected_files`, never `planned_changes`: see this
    module's own docstring for why `PlannedChange.paths` (the Planner
    model's own unvalidated claim) is never a safe authorization source.

    Every path on both sides is normalized through `tools.file_tools.
    relative_path()` -- the same function `tools.executor.ToolExecutor`
    itself uses to validate every real mutation target -- before
    comparison, never a second, parallel canonicalization scheme:
    `allowed_scope` entries are normalized with `allow_root=True` (the
    same flag `ToolExecutor._facts()` already uses for `tool_policy.
    scope`, so a caller-supplied `"."` is a well-formed value here, not a
    malformed one -- it simply then correctly fails the membership check
    below, since a real per-file `affected_files` entry is never
    literally `"."`); `affected_files` paths are normalized strictly
    (`allow_root=False`) since a genuine per-file path is never `"."`
    itself, and one that fails this normalization (absolute, `../`
    traversal, empty segment, embedded control character, `.git`
    component -- see `relative_path()`'s own checks) can never
    legitimately authorize anything and is simply excluded from the
    authorized set, rather than aborting validation for an otherwise
    legitimate plan over one malformed evidence entry. A raw
    `allowed_scope` entry that fails normalization fails this function
    closed outright, with a distinct `malformed_scope_path` reason.

    Set-based comparison also closes duplicate-path ambiguity for free
    (repeated entries on either side collapse to one), and applies no
    case-folding of any kind, matching this codebase's own Linux-only,
    case-sensitive path handling everywhere else.

    If the plan's own evidence never authorized any path for mutation at
    all (no `affected_files` entries, or only `INSPECT` ones), this
    function has no safe authorization to grant regardless of what
    `allowed_scope` asks for -- every non-empty request fails closed,
    matching this module's own "if Planner evidence cannot express safe
    path authorization, fail closed" rule."""
    plan_paths: set[str] = set()
    for affected in content.affected_files:
        if affected.action not in _MUTATING_ACTIONS:
            continue
        try:
            plan_paths.add(files.relative_path(affected.path))
        except ToolError:
            # A malformed/unsafe evidence path can never legitimately
            # authorize anything -- excluded, not fatal to the plan.
            continue
    try:
        requested = {files.relative_path(scope, allow_root=True) for scope in allowed_scope}
    except ToolError as exc:
        raise PipelineContractError(
            f"insufficient_coder_scope:malformed_scope_path:{exc}",
        ) from exc
    excess = sorted(requested - plan_paths)
    if excess:
        raise PipelineContractError(
            f"insufficient_coder_scope:scope_exceeds_planner_authorization:{excess}",
        )
