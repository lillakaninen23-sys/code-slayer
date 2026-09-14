"""The provider-independent worker protocol boundary, and durable worker
trust (Phase 7.1/7.2 — `docs/ROADMAP.md#local-worker-runtime`,
`docs/CODE_SLAYER_VISION.md` §40-42, §58).

Phase 7.1 establishes the model/protocol boundary: immutable request/
response structures, the minimal `WorkerAdapter` interface, strict
structural validation, and a deterministic offline test double. Phase
7.2 adds durable, append-only, evidence-gated worker trust (`LOCKED`/
`GUARDED`/`AUTO`) on top of it — still without any real model,
conformance runner, job worktree, or Prompt Analyst/Question Gate; see
`docs/ROADMAP.md`'s Local Worker Runtime stage for what remains
deferred."""

from code_slayer.store.worker_trust_repo import TrustLevel
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
from code_slayer.workers.trust import TrustResult, WorkerTrustManager

__all__ = [
    "FakeWorkerAdapter",
    "FakeWorkerAdapterError",
    "TrustLevel",
    "TrustResult",
    "ValidationOutcome",
    "ValidationResult",
    "WorkerAdapter",
    "WorkerAdapterError",
    "WorkerRequest",
    "WorkerResponse",
    "WorkerResponseKind",
    "WorkerToolCall",
    "WorkerTrustManager",
    "validate_response",
]
