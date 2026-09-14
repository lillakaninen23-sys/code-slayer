"""The provider-independent worker protocol boundary (Phase 7.1 —
`docs/ROADMAP.md#local-worker-runtime`, `docs/CODE_SLAYER_VISION.md`
§40-42, §58).

Establishes the model/protocol boundary only: immutable request/response
structures, the minimal `WorkerAdapter` interface, strict structural
validation, and a deterministic offline test double. No real model is
integrated, trusted, or executed here — see `docs/ROADMAP.md`'s Local
Worker Runtime stage for what remains deferred (model adapter/
conformance, trust levels, isolated job worktrees, Prompt Analyst/
Question Gate)."""

from code_slayer.workers.fake_adapter import FakeWorkerAdapter, FakeWorkerAdapterError
from code_slayer.workers.protocol import (
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)
from code_slayer.workers.protocol_validation import (
    ValidationOutcome,
    ValidationResult,
    validate_response,
)

__all__ = [
    "FakeWorkerAdapter",
    "FakeWorkerAdapterError",
    "ValidationOutcome",
    "ValidationResult",
    "WorkerAdapter",
    "WorkerAdapterError",
    "WorkerRequest",
    "WorkerResponse",
    "WorkerResponseKind",
    "WorkerToolCall",
    "validate_response",
]
