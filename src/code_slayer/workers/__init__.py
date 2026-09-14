"""The provider-independent worker protocol boundary, durable worker
trust, durable worker conformance, the first real worker adapter, and
the first real qualified-worker execution path (Phase 7.1/7.2/7.3/
7.4a/7.4b/7.4c/7.4h/7.5a — `docs/ROADMAP.md#local-worker-runtime`,
`docs/CODE_SLAYER_VISION.md` §40-43, §58).

Phase 7.1 establishes the model/protocol boundary: immutable request/
response structures, the minimal `WorkerAdapter` interface, strict
structural validation, and a deterministic offline test double. Phase
7.2 adds durable, append-only, evidence-gated worker trust (`LOCKED`/
`GUARDED`/`AUTO`). Phase 7.3 adds the durable conformance-run evidence
that actually justifies a `LOCKED -> GUARDED` promotion —
`workers.promotion.promote_from_conformance` is the intended normal
route, not `WorkerTrustManager.promote_to_guarded` with a hand-picked
string. Phase 7.4a adds `OpenAICompatibleAdapter`, the first real
(non-fake) `WorkerAdapter` — a provider-neutral HTTP client for any
local OpenAI-compatible chat-completions endpoint, still read-only,
still starting every worker `LOCKED`. Phase 7.4b separates genuine
worker-behavior conformance (what `run_conformance_suite()` now
executes and what promotion can be earned from) from Code Slayer's own
safety-regression checks (malformed-protocol rejection, timeout
containment), which remain fully tested but no longer require a
healthy real worker to misbehave on demand — see `workers.conformance`'s
module docstring. Phase 7.4c adds `ToolRequirement` (`OPTIONAL`/
`REQUIRED`) so the `structured_tool_call` conformance case can
explicitly require a genuine structured tool call — never inferred from
`allowed_tools` alone — plus deterministic generation controls
(`OpenAICompatibleConfig.temperature`, default `0.0`) so conformance
evidence is reproducible; see `workers.openai_compatible_adapter`'s
module docstring for the runtime-compatibility findings this produced.
Phase 7.4h replaces `structured_tool_call`'s prompt with a short,
direct, provider-neutral imperative after diagnostic evidence showed
the prior "Code Slayer conformance check: ..." framing measurably
suppressed structured tool use on a real runtime that otherwise
supports it reliably; see `workers.conformance`'s module docstring.
Phase 7.5a adds `workers.execution.execute_guarded_turn`, the first
path from a real, structurally-validated `WorkerToolCall` to the real
`PolicyEngine`/`ToolExecutor` — bounded to one bounded turn, one
capability (`read_file`), and only for a worker/role/capability scope
that has already earned `GUARDED` trust through real conformance
evidence; see `workers.execution`'s module docstring. Still without
job-worktree mutation, mutation-capability trust, `AUTO` trust, or
Prompt Analyst/Question Gate; see `docs/ROADMAP.md`'s Local Worker
Runtime stage for what remains deferred."""

from code_slayer.store.conformance_repo import ConformanceRunStatus
from code_slayer.store.worker_trust_repo import TrustLevel
from code_slayer.workers.conformance import (
    CaseKind,
    ConformanceSuiteResult,
    run_conformance_suite,
)
from code_slayer.workers.execution import TurnOutcome, execute_guarded_turn
from code_slayer.workers.fake_adapter import FakeWorkerAdapter, FakeWorkerAdapterError
from code_slayer.workers.openai_compatible_adapter import (
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
)
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.protocol import (
    ToolRequirement,
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
    "OpenAICompatibleAdapter",
    "OpenAICompatibleConfig",
    "ToolRequirement",
    "TrustLevel",
    "TrustResult",
    "TurnOutcome",
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
    "execute_guarded_turn",
    "promote_from_conformance",
    "run_conformance_suite",
    "validate_response",
]
