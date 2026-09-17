"""The provider-independent worker protocol boundary, durable worker
trust, durable worker conformance, the first real worker adapter, the
first real qualified-worker execution path, and the Prompt Analyst /
Question Gate (Phase 7.1/7.2/7.3/7.4a/7.4b/7.4c/7.4h/7.5a/7.5b/7.6 —
`docs/ROADMAP.md#local-worker-runtime`, `docs/CODE_SLAYER_VISION.md`
§32-33, §40-43, §58). Isolated job worktrees (Phase 7.5c) live in
`repo.job_worktree`, outside this package.

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
evidence; see `workers.execution`'s module docstring. Phase 7.5b binds
that turn's continuation to the exact bytes `ToolExecutor` itself
already read and durably persisted for that operation, retrieved by its
own `output_hash` from the same content-addressed evidence store,
instead of the original approach of independently reopening the
repository file a second time afterward — closing a TOCTOU/provenance
gap without weakening or duplicating `ToolExecutor`'s own authority; see
`workers.execution`'s module docstring. Phase 7.6 adds the Prompt
Analyst (`workers.prompt_analysis`) and Question Gate (`workers.
question_gate`): a provider-neutral, deterministic-first structured
analysis of the original user prompt, and a pure `SUPPRESS`/`ASK`
decision built only from the exact original prompt, that analysis, and
independently supplied `ResolutionEvidence` — never from a model's own
claim, model consensus, or an analyst self-resolving its own proposed
ambiguity. A same-phase hardening follow-up closed exactly that
loophole: `Ambiguity.evidence_keys`/`resolved_by_prompt_substring`/
`risk_class` are analyst-supplied hints only, never themselves
sufficient to suppress a question, and `DESTRUCTIVE`/
`EXTERNAL_SIDE_EFFECT` ambiguities require an explicit `AUTHORIZATION`-
kind resolution from `ORIGINAL_PROMPT`/`DURABLE_TASK_EVIDENCE` — a
repository/runtime fact alone is never authorization; see `workers.
question_gate`'s module docstring. `workers.prompt_provenance` durably
records a decision already made, reusing `ContentStore`/`AuditWriter`,
no schema migration. Neither component can mutate files, execute tools,
bypass `PolicyEngine`, or grant trust; this phase does not yet wire
either into a real task runner (`docs/ROADMAP.md`'s Local Worker Runtime
stage, step 5) — that integration, along with `AUTO` trust and
mutation-capability trust, is still deferred."""

from code_slayer.store.conformance_repo import ConformanceRunStatus
from code_slayer.store.worker_trust_repo import TrustLevel
from code_slayer.workers.conformance import (
    CaseKind,
    ConformanceSuiteResult,
    run_conformance_suite,
)
from code_slayer.workers.execution import TurnOutcome, execute_guarded_turn
from code_slayer.workers.fake_adapter import FakeWorkerAdapter, FakeWorkerAdapterError
from code_slayer.workers.fake_prompt_analyst import (
    FakePromptAnalyst,
    FakePromptAnalystError,
)
from code_slayer.workers.openai_compatible_adapter import (
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
)
from code_slayer.workers.production_eligibility import (
    EligibilityDecision,
    evaluate_production_eligibility,
)
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceContext,
    EvidenceItem,
    EvidenceSource,
    PromptAnalysis,
    PromptAnalysisFields,
    PromptAnalyst,
    PromptAnalystError,
    hash_original_prompt,
    parse_prompt_analysis_output,
)
from code_slayer.workers.prompt_provenance import PromptProvenance, record_prompt_analysis
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
from code_slayer.workers.protocol_normalization import (
    NormalizationOutcome,
    ToolProtocolNormalizationResult,
    ToolProtocolNormalizerRegistry,
)
from code_slayer.workers.protocol_validation import (
    ValidationOutcome,
    ValidationResult,
    validate_response,
)
from code_slayer.workers.question_gate import (
    GateDecision,
    QuestionGate,
    QuestionGateResult,
    ResolutionEvidence,
    ResolutionKind,
)
from code_slayer.workers.qwen_textual_tool_normalizer import QwenTextualToolNormalizer
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleCertificationResult,
    RoleQualificationOutcome,
    record_role_certificate,
)
from code_slayer.workers.security_baseline import (
    BASELINE_VERSION,
    HardDisqualifierCategory,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    SecurityCertificationResult,
    record_baseline_certificate,
)
from code_slayer.workers.trust import TrustResult, WorkerTrustManager
from code_slayer.workers.worker_prompt_analyst import WorkerAdapterPromptAnalyst

__all__ = [
    "BASELINE_VERSION",
    "Ambiguity",
    "AmbiguityRiskClass",
    "CaseKind",
    "ConformanceRunStatus",
    "ConformanceSuiteResult",
    "EligibilityDecision",
    "EvidenceContext",
    "EvidenceItem",
    "EvidenceSource",
    "FakePromptAnalyst",
    "FakePromptAnalystError",
    "FakeWorkerAdapter",
    "FakeWorkerAdapterError",
    "GateDecision",
    "HardDisqualifierCategory",
    "NormalizationOutcome",
    "OpenAICompatibleAdapter",
    "OpenAICompatibleConfig",
    "ProductionRole",
    "PromptAnalysis",
    "PromptAnalysisFields",
    "PromptAnalyst",
    "PromptAnalystError",
    "PromptProvenance",
    "QuestionGate",
    "QuestionGateResult",
    "QwenTextualToolNormalizer",
    "ResolutionEvidence",
    "ResolutionKind",
    "RoleCertificationResult",
    "RoleQualificationOutcome",
    "RuntimeProfileIdentity",
    "SecurityBaselineOutcome",
    "SecurityCertificationResult",
    "ToolRequirement",
    "ToolProtocolNormalizationResult",
    "ToolProtocolNormalizerRegistry",
    "TrustLevel",
    "TrustResult",
    "TurnOutcome",
    "ValidationOutcome",
    "ValidationResult",
    "WorkerAdapter",
    "WorkerAdapterError",
    "WorkerAdapterPromptAnalyst",
    "WorkerRequest",
    "WorkerResponse",
    "WorkerResponseKind",
    "WorkerToolCall",
    "WorkerToolResult",
    "WorkerTrustManager",
    "evaluate_production_eligibility",
    "execute_guarded_turn",
    "hash_original_prompt",
    "parse_prompt_analysis_output",
    "promote_from_conformance",
    "record_baseline_certificate",
    "record_prompt_analysis",
    "record_role_certificate",
    "run_conformance_suite",
    "validate_response",
]
