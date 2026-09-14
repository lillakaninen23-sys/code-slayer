"""The provider-independent worker protocol boundary, durable worker
trust, and durable worker conformance (Phase 7.1/7.2/7.3 —
`docs/ROADMAP.md#local-worker-runtime`, `docs/CODE_SLAYER_VISION.md`
§40-43, §58).

Phase 7.1 establishes the model/protocol boundary: immutable request/
response structures, the minimal `WorkerAdapter` interface, strict
structural validation, and a deterministic offline test double. Phase
7.2 adds durable, append-only, evidence-gated worker trust (`LOCKED`/
`GUARDED`/`AUTO`). Phase 7.3 adds the durable conformance-run evidence
that actually justifies a `LOCKED -> GUARDED` promotion —
`workers.promotion.promote_from_conformance` is the intended normal
route, not `WorkerTrustManager.promote_to_guarded` with a hand-picked
string. Still without any real model, job worktree, mutation-capability
trust, `AUTO` trust, or Prompt Analyst/Question Gate; see
`docs/ROADMAP.md`'s Local Worker Runtime stage for what remains
deferred."""

from code_slayer.store.conformance_repo import ConformanceRunStatus
from code_slayer.store.worker_trust_repo import TrustLevel
from code_slayer.workers.conformance import (
    CaseKind,
    ConformanceSuiteResult,
    run_conformance_suite,
)
from code_slayer.workers.fake_adapter import FakeWorkerAdapter, FakeWorkerAdapterError
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.protocol import (
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
    WorkerToolResult,
)
from code_slayer.workers.protocol_validation import (
    ValidationOutcome,
    ValidationResult,
    validate_response,
)
from code_slayer.workers.trust import TrustResult, WorkerTrustManager

__all__ = [
    "CaseKind",
    "ConformanceRunStatus",
    "ConformanceSuiteResult",
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
    "WorkerToolResult",
    "WorkerTrustManager",
    "promote_from_conformance",
    "run_conformance_suite",
    "validate_response",
]
