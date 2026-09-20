"""Engineering role V1 evidence policy; extends the existing role registry.

Frozen evidence schema. No historical Planner or runtime spec is reinterpreted.
Only code-owned qualification runners produce evidence; models cannot issue it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from code_slayer.audit.canonical import canonical_json
from code_slayer.store.content_store import ContentStore
from code_slayer.workers.role_qualification import (
    ProductionRole,
    canonical_role_evaluation_spec,
    fingerprint_role_evaluation,
)
from code_slayer.workers.security_baseline import (
    canonical_runtime_config_spec,
    canonical_runtime_identity_spec,
    fingerprint_runtime_config,
    fingerprint_runtime_identity,
    require_sha256_hex,
)

ENGINEERING_ROLES = (
    ProductionRole.CODER,
    ProductionRole.REVIEWER,
    ProductionRole.REPAIRER,
    ProductionRole.SECURITY,
)
POLICY_VERSION = "engineering-role-certification-v1"
EVIDENCE_SPEC = "engineering-role-evidence-v1"
EVIDENCE_KIND = "engineering_role_qualification"
MAX_AGE = timedelta(days=30)
MAX_EVIDENCE_BYTES = 65536
# Case identities and semantics are frozen with this schema.
CASE_IDS = {
    ProductionRole.CODER: ("bounded_create", "bounded_update"),
    ProductionRole.REPAIRER: ("repair_explicit_finding", "repair_preserve_unrelated"),
    ProductionRole.REVIEWER: (
        "reject_wrong_operator",
        "reject_missing_validation",
        "accept_correct",
    ),
    ProductionRole.SECURITY: ("reject_auth_bypass", "reject_secret_log", "accept_safe"),
}
CHECKS = {
    ProductionRole.CODER: (
        "structured_contract",
        "correct_tools",
        "scope",
        "mutation",
        "truthful_completion",
        "validation",
        "no_forbidden_attempt",
    ),
    ProductionRole.REPAIRER: (
        "structured_contract",
        "correct_tools",
        "scope",
        "mutation",
        "truthful_completion",
        "validation",
        "no_forbidden_attempt",
        "explicit_findings",
        "unrelated_preserved",
    ),
    ProductionRole.REVIEWER: ("structured_findings", "correct_verdict", "non_mutating"),
    ProductionRole.SECURITY: ("structured_findings", "correct_verdict", "non_mutating"),
}


class RoleEvidenceError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise RoleEvidenceError("malformed_certificate_timestamp") from exc


def check_freshness(started_at, evaluated_at, issued_at, now):
    start, end, issued, current = map(timestamp, (started_at, evaluated_at, issued_at, now))
    if not start <= end <= issued <= current:
        raise RoleEvidenceError("certificate_timestamp_mismatch")
    if current - start >= MAX_AGE:
        raise RoleEvidenceError("certificate_expired")


def runtime_spec(profile):
    return canonical_runtime_identity_spec(
        **{
            key: getattr(profile, key)
            for key in (
                "model_tag",
                "model_digest",
                "endpoint",
                "runtime_version",
                "normalizer_id",
                "normalizer_version",
                "effective_context_tokens",
                "temperature",
            )
        }
    )


def evaluation_spec(evaluation):
    values = asdict(evaluation)
    values.pop("role_evaluation_fingerprint")
    return canonical_role_evaluation_spec(**values)


def build_document(worker_id, profile, evaluation, cases, started_at, evaluated_at):
    common = runtime_spec(profile)
    config = canonical_runtime_config_spec(
        **{k: v for k, v in common.items() if k != "spec_version"},
        output_token_budget=evaluation.output_token_budget,
        tool_choice_enforcement=evaluation.tool_choice_enforcement,
    )
    passed = all(all(case["checks"].values()) for case in cases)
    return {
        "spec_version": EVIDENCE_SPEC,
        "policy_version": POLICY_VERSION,
        "worker_id": worker_id,
        "role": evaluation.role.value,
        "runtime_identity_spec": common,
        "runtime_identity_fingerprint": profile.runtime_identity_fingerprint,
        "runtime_config_spec": config,
        "runtime_config_fingerprint": fingerprint_runtime_config(config),
        "role_evaluation_spec": evaluation_spec(evaluation),
        "role_evaluation_fingerprint": evaluation.role_evaluation_fingerprint,
        "started_at": started_at,
        "evaluated_at": evaluated_at,
        "provenance": "code-owned-engineering-fixtures-v1",
        "cases": cases,
        "outcome": "PASS" if passed else "FAIL",
        "reason": "qualification_pass" if passed else "qualification_disqualified",
    }


def verify_document(document):
    """Recompute all bindings and aggregate checks under the frozen V1 schema."""
    try:
        expected_keys = {
            "spec_version",
            "policy_version",
            "worker_id",
            "role",
            "runtime_identity_spec",
            "runtime_identity_fingerprint",
            "runtime_config_spec",
            "runtime_config_fingerprint",
            "role_evaluation_spec",
            "role_evaluation_fingerprint",
            "started_at",
            "evaluated_at",
            "provenance",
            "cases",
            "outcome",
            "reason",
        }
        if not isinstance(document, dict) or set(document) != expected_keys:
            raise RoleEvidenceError("malformed_role_evidence")
        role = ProductionRole(document["role"])
        if role not in ENGINEERING_ROLES or document["spec_version"] != EVIDENCE_SPEC:
            raise RoleEvidenceError("unsupported_role_evidence_schema")
        if document["policy_version"] != POLICY_VERSION:
            raise RoleEvidenceError("role_certificate_policy_version_stale")
        if document["provenance"] != "code-owned-engineering-fixtures-v1":
            raise RoleEvidenceError("malformed_role_evidence")
        if not isinstance(document["worker_id"], str) or not document["worker_id"]:
            raise RoleEvidenceError("malformed_role_evidence")
        common = document["runtime_identity_spec"]
        canonical = canonical_runtime_identity_spec(
            **{k: v for k, v in common.items() if k != "spec_version"},
        )
        if (
            common != canonical
            or fingerprint_runtime_identity(common) != document["runtime_identity_fingerprint"]
        ):
            raise RoleEvidenceError("role_evidence_runtime_mismatch")
        evaluation = document["role_evaluation_spec"]
        canonical = canonical_role_evaluation_spec(
            **{
                k: (role if k == "role" else v)
                for k, v in evaluation.items()
                if k != "spec_version"
            },
        )
        if (
            evaluation != canonical
            or evaluation["role"] != role.value
            or evaluation["policy_version"] != POLICY_VERSION
            or evaluation["runtime_identity_fingerprint"]
            != document["runtime_identity_fingerprint"]
            or fingerprint_role_evaluation(evaluation) != document["role_evaluation_fingerprint"]
        ):
            raise RoleEvidenceError("role_evidence_evaluation_mismatch")
        config = canonical_runtime_config_spec(
            **{k: v for k, v in common.items() if k != "spec_version"},
            output_token_budget=evaluation["output_token_budget"],
            tool_choice_enforcement=evaluation["tool_choice_enforcement"],
        )
        if (
            config != document["runtime_config_spec"]
            or fingerprint_runtime_config(config) != document["runtime_config_fingerprint"]
        ):
            raise RoleEvidenceError("role_evidence_config_mismatch")
        cases = document["cases"]
        if not isinstance(cases, list) or [c["case_id"] for c in cases] != list(CASE_IDS[role]):
            raise RoleEvidenceError("missing_role_qualification_case")
        for case in cases:
            if set(case) != {"case_id", "checks", "observation_hash"}:
                raise RoleEvidenceError("malformed_role_evidence")
            require_sha256_hex("observation_hash", case["observation_hash"])
            if set(case["checks"]) != set(CHECKS[role]) or any(
                type(v) is not bool for v in case["checks"].values()
            ):
                raise RoleEvidenceError("malformed_role_evidence")
        passed = all(all(case["checks"].values()) for case in cases)
        if document["outcome"] != ("PASS" if passed else "FAIL") or document["reason"] != (
            "qualification_pass" if passed else "qualification_disqualified"
        ):
            raise RoleEvidenceError("role_evidence_aggregate_mismatch")
        if timestamp(document["started_at"]) > timestamp(document["evaluated_at"]):
            raise RoleEvidenceError("certificate_timestamp_mismatch")
    except RoleEvidenceError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RoleEvidenceError("malformed_role_evidence") from exc
    return document


def read_role_evidence(conn, blobs_dir, evidence_ref):
    try:
        require_sha256_hex("evidence_ref", evidence_ref)
        store = ContentStore(conn, blobs_dir)
        meta = store.get_meta(evidence_ref)
        if (
            meta is None
            or meta.source_kind != EVIDENCE_KIND
            or meta.exportable
            or meta.truncated
            or meta.byte_size > MAX_EVIDENCE_BYTES
        ):
            raise RoleEvidenceError("missing_or_malformed_role_evidence")
        data = store.read(evidence_ref)
        if hashlib.sha256(data).hexdigest() != evidence_ref:
            raise RoleEvidenceError("role_evidence_hash_mismatch")
        if len(data) > MAX_EVIDENCE_BYTES:
            raise RoleEvidenceError("malformed_role_evidence")
        document = verify_document(json.loads(data))
        if canonical_json(document).encode() != data:
            raise RoleEvidenceError("malformed_role_evidence")
        return document
    except RoleEvidenceError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RoleEvidenceError("missing_or_malformed_role_evidence") from exc


def verify_engineering_certificates(conn, blobs_dir, role_certificate, security_certificate, now):
    # Deferred import avoids the existing security/planning package initialization cycle.
    from code_slayer.security.evidence import read_baseline_security_evidence

    if blobs_dir is None:
        raise RoleEvidenceError("missing_durable_role_evidence")
    cert = role_certificate
    if cert.policy_version != POLICY_VERSION:
        raise RoleEvidenceError("role_certificate_policy_version_stale")
    document = read_role_evidence(conn, blobs_dir, cert.evidence_ref)
    for key in (
        "worker_id",
        "role",
        "policy_version",
        "runtime_identity_fingerprint",
        "runtime_config_fingerprint",
        "role_evaluation_fingerprint",
        "outcome",
        "reason",
    ):
        if document[key] != getattr(cert, key):
            raise RoleEvidenceError("role_certificate_evidence_mismatch")
    if cert.classification != document["outcome"]:
        raise RoleEvidenceError("role_certificate_evidence_mismatch")
    check_freshness(document["started_at"], document["evaluated_at"], cert.issued_at, now)
    try:
        require_sha256_hex("evidence_ref", security_certificate.evidence_ref)
        baseline = read_baseline_security_evidence(
            conn,
            blobs_dir,
            security_certificate.evidence_ref,
            expected_runtime_identity_fingerprint=cert.runtime_identity_fingerprint,
        )
        if (
            baseline["worker_id"] != cert.worker_id
            or baseline["final_outcome"] != security_certificate.outcome
            or (
                baseline["final_reason"] != security_certificate.reason
                and not (
                    security_certificate.reason == "promoted_from_validation"
                    and security_certificate.promoted_from_validation_certificate_id
                )
            )
            or baseline["hard_disqualifiers"]
            != json.loads(security_certificate.hard_disqualifiers_json)
        ):
            raise RoleEvidenceError("baseline_certificate_evidence_mismatch")
        check_freshness(
            baseline["started_at"], baseline["ended_at"], security_certificate.issued_at, now
        )
    except RoleEvidenceError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RoleEvidenceError("missing_or_malformed_baseline_evidence") from exc
    return document
