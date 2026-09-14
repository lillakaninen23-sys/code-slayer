"""A deterministic, fully offline `WorkerAdapter` test double (Phase 7.1).

`FakeWorkerAdapter` never touches the network, a subprocess, the
filesystem, or a real model — it exists so the protocol/validation
boundary, and everything built on top of it later, can be tested without
any real inference dependency (the same "deterministic fake before real
model judgment" pattern `lease.manager.LeaseManager`'s injectable
`liveness_fn`/`now_fn` already established for process liveness).

Each call to `infer()` returns the next canned `WorkerResponse` (or
raises the next canned exception) from a fixed, caller-supplied
sequence, in order. There is no randomness, no network, no hidden state
beyond "which canned entry is next" — the same request always advances
to the same next canned outcome, deterministically.
"""

from __future__ import annotations

from collections.abc import Sequence

from code_slayer.workers.protocol import WorkerRequest, WorkerResponse


class FakeWorkerAdapterError(RuntimeError):
    """A canned "the call failed entirely" outcome — no response was
    ever received, mirroring what `WorkerAdapterError` (`protocol.py`)
    represents for a real adapter's transport/timeout failure. Distinct
    from a canned `WorkerResponse(kind=MALFORMED)`, which *is* a
    definite (fake) response the caller received."""


class FakeWorkerAdapter:
    """Deterministic `WorkerAdapter` implementation backed by a fixed,
    in-memory list of canned outcomes."""

    def __init__(self, responses: Sequence[WorkerResponse | Exception]) -> None:
        self._responses: list[WorkerResponse | Exception] = list(responses)
        self._calls: list[WorkerRequest] = []

    @property
    def calls(self) -> tuple[WorkerRequest, ...]:
        """Every request this fake has received, in order — for tests to
        assert on what was actually asked, never inferred."""
        return tuple(self._calls)

    def infer(self, request: WorkerRequest) -> WorkerResponse:
        if not isinstance(request, WorkerRequest):
            raise TypeError("FakeWorkerAdapter.infer() requires a WorkerRequest")
        self._calls.append(request)
        if not self._responses:
            raise FakeWorkerAdapterError("fake_adapter_exhausted_canned_responses")
        next_outcome = self._responses.pop(0)
        if isinstance(next_outcome, Exception):
            raise next_outcome
        return next_outcome
