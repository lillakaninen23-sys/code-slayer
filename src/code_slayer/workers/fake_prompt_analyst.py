"""A deterministic, fully offline `PromptAnalyst` test double (Phase 7.6).

Mirrors `workers.fake_adapter.FakeWorkerAdapter`'s own pattern: a fixed,
caller-supplied sequence of canned outcomes, consumed in order, no
randomness, no network, no real model. `responses` is deliberately typed
as `Sequence[object]`, not `Sequence[PromptAnalysis]`: a test proving
`workers.question_gate.QuestionGate` fails closed on a malformed analyst
result needs to be able to queue something that is *not* a
`PromptAnalysis` at all (`None`, a plain dict, ...) — this fake never
validates its own canned responses, exactly like `FakeWorkerAdapter`
never validates its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from code_slayer.workers.prompt_analysis import EvidenceItem, PromptAnalysis


class FakePromptAnalystError(RuntimeError):
    """A canned sequence was exhausted — every real call this fake ever
    receives must have a corresponding queued outcome; there is no
    default or synthesized response."""


class FakePromptAnalyst:
    """Deterministic `PromptAnalyst` implementation backed by a fixed,
    in-memory list of canned outcomes."""

    def __init__(self, responses: Sequence[object]) -> None:
        self._responses: list[object] = list(responses)
        self._calls: list[tuple[str, Mapping[str, EvidenceItem]]] = []

    @property
    def calls(self) -> tuple[tuple[str, Mapping[str, EvidenceItem]], ...]:
        """Every `(original_prompt, evidence)` pair this fake actually
        received, in order — for tests to assert on what was really
        asked, never inferred."""
        return tuple(self._calls)

    def analyze(
        self, original_prompt: str, evidence: Mapping[str, EvidenceItem],
    ) -> PromptAnalysis:
        self._calls.append((original_prompt, evidence))
        if not self._responses:
            raise FakePromptAnalystError("fake_prompt_analyst_exhausted_canned_responses")
        return self._responses.pop(0)
