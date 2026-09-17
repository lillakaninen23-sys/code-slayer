"""Baseline Security Evaluation: the dedicated, role-independent
harness that produces durable evidence of whether a worker/runtime
exhibited unsafe security-relevant behavior.

This package is the *evaluation* subsystem. Certificate recording
lives in `workers.security_baseline` and is deliberately not invoked
here. Running an evaluation never issues a Baseline Security
certificate, never records a role certificate, never alters trust or
permissions, and never activates production eligibility.
"""

from code_slayer.security.evaluation import (
    EVALUATION_SUITE_VERSION,
    SecurityCase,
    SecurityCaseOutcome,
    SecurityCaseResult,
    SecurityEvaluationResult,
    SecurityHarnessExecutor,
    mandatory_cases,
    run_baseline_security_evaluation,
)
from code_slayer.security.evidence import (
    EVIDENCE_KIND,
    EVIDENCE_SPEC_VERSION,
    SecurityEvaluationEvidenceError,
    persist_baseline_security_evidence,
    read_baseline_security_evidence,
)
from code_slayer.security.transport import (
    CANARY_TOOL_SCHEMAS,
    SecurityEvaluationAdapter,
    apply_configured_protocol_normalizer,
    build_security_evaluation_normalizer_registry,
    security_evaluation_tool_schemas,
    validate_canary_tool_params,
    validate_security_evaluation_transport_binding,
)

__all__ = [
    "CANARY_TOOL_SCHEMAS",
    "EVALUATION_SUITE_VERSION",
    "EVIDENCE_KIND",
    "EVIDENCE_SPEC_VERSION",
    "SecurityCase",
    "SecurityCaseOutcome",
    "SecurityCaseResult",
    "SecurityEvaluationAdapter",
    "SecurityEvaluationEvidenceError",
    "SecurityEvaluationResult",
    "SecurityHarnessExecutor",
    "apply_configured_protocol_normalizer",
    "build_security_evaluation_normalizer_registry",
    "mandatory_cases",
    "persist_baseline_security_evidence",
    "read_baseline_security_evidence",
    "run_baseline_security_evaluation",
    "security_evaluation_tool_schemas",
    "validate_canary_tool_params",
    "validate_security_evaluation_transport_binding",
]
