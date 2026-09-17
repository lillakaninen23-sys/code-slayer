"""Production live Baseline Security certification runner.

This module is the production bridge:

    live Ollama runtime verification
            ↓
    construct exact RuntimeProfileIdentity
            ↓
    construct bound SecurityEvaluationAdapter
            ↓
    run_baseline_security_evaluation()
            ↓
    fresh durable evidence_ref
            ↓
    read_baseline_security_evidence()
            ↓
    derive outcome/reason/hard categories FROM reread evidence
            ↓
    record_baseline_certificate()

It never accepts a caller-supplied `WorkerAdapter`, evaluation result,
`evidence_ref`, outcome, or hard-disqualifier list. Those values are
created and owned here.

`workers.security_baseline.record_baseline_certificate()` remains the
low-level durable recorder. This module is the only production-facing
path that is allowed to decide those recorder inputs from live evidence.

This module is role-independent. It does not import Planner policy,
does not grant trust or permissions, and does not activate a worker.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from code_slayer.security.evaluation import run_baseline_security_evaluation
from code_slayer.security.evidence import (
    SecurityEvaluationEvidenceError,
    read_baseline_security_evidence,
)
from code_slayer.security.transport import (
    SecurityEvaluationAdapter,
    validate_security_evaluation_transport_binding,
)
from code_slayer.store.db import utcnow_iso
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.openai_compatible_adapter import OpenAICompatibleConfig
from code_slayer.workers.security_baseline import (
    HardDisqualifierCategory,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
_DEFAULT_PROBE_MAX_RESPONSE_BYTES = 65536


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect — a 3xx is a hard probe failure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise urllib.error.HTTPError(newurl, code, "redirects are not followed", headers, fp)


@dataclass(frozen=True)
class LiveOllamaRuntimeExpectation:
    """Caller-owned expected Ollama runtime. Nothing here is a default
    host, model, digest, or fingerprint — the operational caller
    supplies the exact values that must be live-verified.

    `effective_context_tokens` is trusted deployment configuration.
    The Ollama version/tags probe does not measure context capacity;
    it is bound by the exact model digest plus the expected runtime
    fingerprint instead.

    `ollama_root` is the Ollama HTTP root (`http://HOST:PORT`), never
    the OpenAI-compatible `/v1` path. The OpenAI base URL is derived
    as `{ollama_root}/v1` so the probe and inference cannot silently
    target different hosts.
    """

    ollama_root: str
    model_tag: str
    model_digest: str
    runtime_version: str
    effective_context_tokens: int
    temperature: float
    expected_runtime_identity_fingerprint: str
    normalizer_id: str | None = None
    normalizer_version: int | None = None
    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    probe_max_response_bytes: int = _DEFAULT_PROBE_MAX_RESPONSE_BYTES
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ollama_root, str) or not self.ollama_root.strip():
            raise ValueError("ollama_root must be a non-empty string")
        root = self.ollama_root.strip().rstrip("/")
        if root.endswith("/v1"):
            raise ValueError("ollama_root_must_not_include_openai_path")
        if not isinstance(self.model_tag, str) or not self.model_tag.strip():
            raise ValueError("model_tag must be a non-empty string")
        if not isinstance(self.model_digest, str) or not self.model_digest.strip():
            raise ValueError("model_digest must be a non-empty string")
        if not isinstance(self.runtime_version, str) or not self.runtime_version.strip():
            raise ValueError("runtime_version must be a non-empty string")
        if (
            not isinstance(self.effective_context_tokens, int)
            or isinstance(self.effective_context_tokens, bool)
            or self.effective_context_tokens < 1
        ):
            raise ValueError("effective_context_tokens must be a positive integer")
        if not isinstance(self.temperature, (int, float)) or not (
            0.0 <= float(self.temperature) <= 2.0
        ):
            raise ValueError("temperature must be between 0.0 and 2.0")
        if (
            not isinstance(self.expected_runtime_identity_fingerprint, str)
            or len(self.expected_runtime_identity_fingerprint) != 64
            or any(
                ch not in "0123456789abcdef"
                for ch in self.expected_runtime_identity_fingerprint
            )
        ):
            raise ValueError("expected_runtime_identity_fingerprint must be a SHA-256 hex digest")
        if (self.normalizer_id is None) != (self.normalizer_version is None):
            raise ValueError(
                "normalizer_id and normalizer_version must both be set or both be None",
            )
        if self.normalizer_id is not None and (
            not isinstance(self.normalizer_id, str) or not self.normalizer_id.strip()
        ):
            raise ValueError("normalizer_id must be a non-empty string or None")
        if self.normalizer_version is not None and (
            not isinstance(self.normalizer_version, int)
            or isinstance(self.normalizer_version, bool)
            or self.normalizer_version < 1
        ):
            raise ValueError("normalizer_version must be a positive integer or None")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if not isinstance(self.max_response_bytes, int) or self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        if (
            not isinstance(self.probe_max_response_bytes, int)
            or self.probe_max_response_bytes <= 0
        ):
            raise ValueError("probe_max_response_bytes must be a positive integer")

    @property
    def normalized_ollama_root(self) -> str:
        return self.ollama_root.strip().rstrip("/")

    @property
    def openai_base_url(self) -> str:
        return f"{self.normalized_ollama_root}/v1"


@dataclass(frozen=True)
class LiveSecurityCertificationResult:
    """Bounded result of one live Baseline Security certification
    attempt. Never carries raw model text, secrets, API keys, or HTTP
    bodies.
    """

    ok: bool
    reason: str
    runtime_identity_fingerprint: str | None = None
    evaluation_evidence_ref: str | None = None
    outcome: SecurityBaselineOutcome | None = None
    hard_disqualifiers: tuple[HardDisqualifierCategory, ...] = ()
    certificate_id: str | None = None


def _deny(
    reason: str,
    *,
    runtime_identity_fingerprint: str | None = None,
    evaluation_evidence_ref: str | None = None,
    outcome: SecurityBaselineOutcome | None = None,
    hard_disqualifiers: tuple[HardDisqualifierCategory, ...] = (),
) -> LiveSecurityCertificationResult:
    return LiveSecurityCertificationResult(
        False,
        reason,
        runtime_identity_fingerprint=runtime_identity_fingerprint,
        evaluation_evidence_ref=evaluation_evidence_ref,
        outcome=outcome,
        hard_disqualifiers=hard_disqualifiers,
    )


def _canonical_digest(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text


def _strict_json_object(raw: bytes) -> dict:
    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise json.JSONDecodeError("duplicate_object_key", "", 0)
            result[key] = value
        return result

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime_probe_malformed_json") from exc
    if not isinstance(parsed, dict):
        raise ValueError("runtime_probe_malformed_json")
    return parsed


def _probe_get(url: str, expectation: LiveOllamaRuntimeExpectation) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=expectation.timeout) as response:
            status = response.status
            raw = response.read(expectation.probe_max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("runtime_probe_redirect") from exc
        raise ValueError("runtime_probe_unavailable") from exc
    except urllib.error.URLError as exc:
        raise ValueError("runtime_probe_unavailable") from exc
    except TimeoutError as exc:
        raise ValueError("runtime_probe_unavailable") from exc
    except OSError as exc:
        raise ValueError("runtime_probe_unavailable") from exc
    if status != 200:
        raise ValueError("runtime_probe_unavailable")
    if len(raw) > expectation.probe_max_response_bytes:
        raise ValueError("runtime_probe_response_too_large")
    return _strict_json_object(raw)


def verify_ollama_runtime(
    expectation: LiveOllamaRuntimeExpectation,
) -> tuple[str, str]:
    """Return `(runtime_version, model_digest)` as live-verified against
    `expectation`. Does not construct an identity and does not infer.
    `effective_context_tokens` is not read from Ollama.
    """
    root = expectation.normalized_ollama_root
    version_doc = _probe_get(f"{root}/api/version", expectation)
    version = version_doc.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("runtime_probe_malformed_json")
    if version != expectation.runtime_version:
        raise ValueError("runtime_version_mismatch")

    tags_doc = _probe_get(f"{root}/api/tags", expectation)
    models = tags_doc.get("models")
    if not isinstance(models, list):
        raise ValueError("runtime_probe_malformed_json")

    matches: list[dict] = []
    for item in models:
        if not isinstance(item, dict):
            raise ValueError("runtime_probe_malformed_json")
        name = item.get("name")
        if name == expectation.model_tag:
            matches.append(item)
    if not matches:
        raise ValueError("runtime_model_missing")
    if len(matches) > 1:
        raise ValueError("runtime_model_duplicate")
    digest = matches[0].get("digest")
    if not isinstance(digest, str) or not digest:
        raise ValueError("runtime_model_digest_mismatch")
    if _canonical_digest(digest) != _canonical_digest(expectation.model_digest):
        raise ValueError("runtime_model_digest_mismatch")
    return version, expectation.model_digest


def _identity_from_verified_expectation(
    expectation: LiveOllamaRuntimeExpectation,
    *,
    runtime_version: str,
    model_digest: str,
) -> RuntimeProfileIdentity:
    return runtime_profile_identity_from_config(
        model_tag=expectation.model_tag,
        model_digest=model_digest,
        endpoint=expectation.openai_base_url,
        runtime_version=runtime_version,
        effective_context_tokens=expectation.effective_context_tokens,
        temperature=float(expectation.temperature),
        normalizer_id=expectation.normalizer_id,
        normalizer_version=expectation.normalizer_version,
    )


def _outcome_from_document(raw: object) -> SecurityBaselineOutcome:
    if not isinstance(raw, str):
        raise ValueError("invalid_final_outcome")
    try:
        return SecurityBaselineOutcome(raw)
    except ValueError as exc:
        raise ValueError("invalid_final_outcome") from exc


def _hard_from_document(raw: object) -> tuple[HardDisqualifierCategory, ...]:
    if not isinstance(raw, list):
        raise ValueError("invalid_hard_category")
    converted: list[HardDisqualifierCategory] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("invalid_hard_category")
        try:
            converted.append(HardDisqualifierCategory(item))
        except ValueError as exc:
            raise ValueError("invalid_hard_category") from exc
    return tuple(converted)


def certify_live_baseline_security(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    blobs_dir: Path | str,
    expected: LiveOllamaRuntimeExpectation,
    now_fn=utcnow_iso,
) -> LiveSecurityCertificationResult:
    """Verify a live Ollama runtime, run the Baseline Security harness
    through a bound `SecurityEvaluationAdapter`, reread durable
    evidence, and record exactly one Baseline Security certificate
    from the reread document.

    PASS, FAIL, and HARD_DISQUALIFIED are all recorded when the
    evaluation completed and evidence verifies. Preflight, probe,
    binding, or evidence-verification failures record nothing.
    """
    if not isinstance(expected, LiveOllamaRuntimeExpectation):
        return _deny("malformed_live_certification_request")
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_live_certification_request")
    if not isinstance(blobs_dir, (str, Path)) or not str(blobs_dir).strip():
        return _deny("missing_durable_security_evidence")
    if WorkersRepo(conn).get(worker_id) is None:
        return _deny("unknown_worker")

    try:
        runtime_version, model_digest = verify_ollama_runtime(expected)
    except ValueError as exc:
        reason = str(exc) if str(exc) else "runtime_probe_unavailable"
        return _deny(reason)

    try:
        runtime_profile = _identity_from_verified_expectation(
            expected,
            runtime_version=runtime_version,
            model_digest=model_digest,
        )
    except (TypeError, ValueError):
        return _deny("insufficient_runtime_profile_identity")

    fingerprint = runtime_profile.runtime_identity_fingerprint
    if fingerprint != expected.expected_runtime_identity_fingerprint:
        return _deny(
            "runtime_identity_fingerprint_mismatch",
            runtime_identity_fingerprint=fingerprint,
        )

    try:
        config = OpenAICompatibleConfig(
            base_url=expected.openai_base_url,
            model=expected.model_tag,
            timeout=expected.timeout,
            api_key=expected.api_key,
            max_response_bytes=expected.max_response_bytes,
            temperature=float(expected.temperature),
        )
        adapter = SecurityEvaluationAdapter(config, runtime_profile=runtime_profile)
    except (TypeError, ValueError) as exc:
        reason = str(exc) if str(exc) else "live_transport_binding_failed"
        return _deny(reason, runtime_identity_fingerprint=fingerprint)
    binding = validate_security_evaluation_transport_binding(adapter, runtime_profile)
    if binding is not None:
        return _deny(binding, runtime_identity_fingerprint=fingerprint)

    evaluation = run_baseline_security_evaluation(
        conn,
        worker_id=worker_id,
        adapter=adapter,
        runtime_profile=runtime_profile,
        blobs_dir=blobs_dir,
        now_fn=now_fn,
    )
    if not evaluation.ok or evaluation.outcome is None:
        return _deny(
            evaluation.reason or "evaluation_could_not_start",
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evaluation.evidence_ref,
        )
    evidence_ref = evaluation.evidence_ref
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        return _deny(
            "missing_durable_security_evidence",
            runtime_identity_fingerprint=fingerprint,
        )
    if evaluation.worker_id != worker_id:
        return _deny(
            "evidence_worker_mismatch",
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
        )
    if evaluation.runtime_identity_fingerprint != fingerprint:
        return _deny(
            "runtime_identity_fingerprint_mismatch",
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
        )

    try:
        document = read_baseline_security_evidence(
            conn,
            blobs_dir,
            evidence_ref,
            expected_runtime_identity_fingerprint=fingerprint,
        )
    except SecurityEvaluationEvidenceError as exc:
        return _deny(
            exc.reason,
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
        )

    if document.get("worker_id") != worker_id:
        return _deny(
            "evidence_worker_mismatch",
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
        )
    if document.get("runtime_identity_fingerprint") != fingerprint:
        return _deny(
            "runtime_identity_fingerprint_mismatch",
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
        )

    try:
        outcome = _outcome_from_document(document.get("final_outcome"))
        hard = _hard_from_document(document.get("hard_disqualifiers"))
    except ValueError as exc:
        return _deny(
            str(exc),
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
        )
    reason = document.get("final_reason")
    if not isinstance(reason, str) or not reason:
        return _deny(
            "malformed_security_evidence",
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
            outcome=outcome,
            hard_disqualifiers=hard,
        )

    recorded = record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=runtime_profile,
        outcome=outcome,
        evidence_ref=evidence_ref,
        reason=reason,
        hard_disqualifiers=hard,
        now_fn=now_fn,
    )
    if not recorded.ok or recorded.certificate is None:
        return _deny(
            recorded.reason,
            runtime_identity_fingerprint=fingerprint,
            evaluation_evidence_ref=evidence_ref,
            outcome=outcome,
            hard_disqualifiers=hard,
        )
    return LiveSecurityCertificationResult(
        True,
        recorded.reason,
        runtime_identity_fingerprint=fingerprint,
        evaluation_evidence_ref=evidence_ref,
        outcome=outcome,
        hard_disqualifiers=hard,
        certificate_id=recorded.certificate.certificate_id,
    )
