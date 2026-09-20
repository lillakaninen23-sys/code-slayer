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
legitimately pass `(".",)` even for a plan whose `planned_changes` only
ever named a single file, silently broadening what the Coder is actually
authorized to touch far past what the Planner evidence supports.

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
from code_slayer.planning.models import EngineeringPlanContent, PlanState
from code_slayer.store.models import EngineeringPlanRow


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
    plan itself actually declared (the union of every `PlannedChange.
    paths` across `content.planned_changes`) -- never broader. Raises
    `PipelineContractError` (fail closed, same `insufficient_coder_scope:
    <reason>` family `validate_planner_handoff()` uses) the moment
    `allowed_scope` claims anything the plan did not.

    Deliberately exact-membership, not prefix/containment: an entry like
    `"src"` or `"."` would legitimize every path underneath it
    (`policy.engine.PolicyEngine`'s own `tools.file_tools.within()` scope
    check is prefix-based), which is exactly the silent broadening this
    function exists to refuse -- even if every path the plan named
    happens to live under that prefix. A caller may request any NON-EMPTY
    subset of the plan's own declared paths (a narrower scope than the
    plan authorizes is always acceptable), never anything outside it.

    If the plan's own `planned_changes` never named any path at all
    (`PlannedChange.paths` defaults to `()`, so a plan can be `READY` with
    change descriptions but no explicit paths), this function has no safe
    authorization to grant regardless of what `allowed_scope` asks for --
    every request fails closed, matching this module's own "if Planner
    evidence cannot express safe path authorization, fail closed" rule."""
    plan_paths = {path for change in content.planned_changes for path in change.paths}
    requested = set(allowed_scope)
    excess = sorted(requested - plan_paths)
    if excess:
        raise PipelineContractError(
            f"insufficient_coder_scope:scope_exceeds_planner_authorization:{excess}",
        )
