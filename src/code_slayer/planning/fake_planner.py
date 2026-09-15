"""A deterministic, fully offline `Planner` test double (Phase 8.2).

Mirrors `workers.fake_prompt_analyst.FakePromptAnalyst`/`workers.
fake_adapter.FakeWorkerAdapter`'s own pattern: a fixed, caller-supplied
sequence of canned outcomes, consumed in order, no randomness, no
network, no real model. `responses` is deliberately typed as
`Sequence[object]`, not `Sequence[PlannerResponse]`: a test proving
`planning.service.EngineeringPlanningService` fails closed on a
malformed planner result needs to be able to queue something that is
*not* a `PlannerResponse` at all — this fake never validates its own
canned responses, exactly like its siblings never validate theirs.

Like `workers.fake_prompt_analyst.FakePromptAnalyst`, this lives under
`code_slayer` (not only under `tests/`) so it can be wired the same way
those existing fakes are — but nothing in this package's own production
wiring (`planning.service`, `api.service.ApplicationService`) ever
selects it automatically; a caller must always explicitly construct and
pass one. Production planning without an explicit, real `Planner`
configured fails closed (`APIError("planner_not_configured", ...)`,
`api.service`), never silently falling back to this fake.
"""

from __future__ import annotations

from collections.abc import Sequence

from code_slayer.planning.planner import PlannerRequest, PlannerResponse


class FakePlannerError(RuntimeError):
    """A canned sequence was exhausted — every real call this fake ever
    receives must have a corresponding queued outcome; there is no
    default or synthesized response."""


class FakePlanner:
    """Deterministic `Planner` implementation backed by a fixed,
    in-memory list of canned outcomes."""

    def __init__(self, responses: Sequence[object]) -> None:
        self._responses: list[object] = list(responses)
        self._calls: list[PlannerRequest] = []

    @property
    def calls(self) -> tuple[PlannerRequest, ...]:
        """Every `PlannerRequest` this fake actually received, in order
        — for tests to assert on exactly what bounded context was
        built, never inferred."""
        return tuple(self._calls)

    def plan(self, request: PlannerRequest) -> PlannerResponse:
        self._calls.append(request)
        if not self._responses:
            raise FakePlannerError("fake_planner_exhausted_canned_responses")
        return self._responses.pop(0)
