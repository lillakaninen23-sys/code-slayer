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
from code_slayer.planning.models import PlanState
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
