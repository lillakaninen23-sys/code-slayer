"""Durable engineering planning (Phase 8.2 —
`docs/ROADMAP.md#engineering-planning`).

Transforms an original user engineering request plus authoritative
Repository Intelligence (Phase 8.1/8.1a) into a structured, durable,
evidence-backed `planning.models.EngineeringPlanContent` — planning
only, never repository mutation, model write tools, command execution,
checkpoint creation, merge/promotion, or AUTO trust (see `planning.
service`'s module docstring for the structural argument).

See `planning.service.EngineeringPlanningService` for the public entry
point every other component (the WebUI application API, a future
execution phase) should use — nothing outside this package queries
`engineering_plans`/`engineering_plan_human_resolutions` or the
underlying content-addressed blobs directly.
"""

from code_slayer.planning.models import (
    AffectedFile,
    AffectedFileAction,
    DiscoveredCommandRef,
    EngineeringPlanContent,
    EvidenceRef,
    OpenQuestion,
    PlannedChange,
    PlanState,
)
from code_slayer.planning.planner import Planner, PlannerRequest, PlannerResponse
from code_slayer.planning.service import EngineeringPlanningService, PlanRecord

__all__ = [
    "AffectedFile",
    "AffectedFileAction",
    "DiscoveredCommandRef",
    "EngineeringPlanContent",
    "EngineeringPlanningService",
    "EvidenceRef",
    "OpenQuestion",
    "PlanRecord",
    "PlanState",
    "PlannedChange",
    "Planner",
    "PlannerRequest",
    "PlannerResponse",
]
