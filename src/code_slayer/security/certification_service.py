"""Certification Center application service.

WebUI/API is a control surface. This module:

- reads production worker/certificate/eligibility state
- runs preflight without model inference
- accepts exactly one durable validation run per operator action
- delegates live certification to `certify_live_baseline_security`
- writes certificates and evidence only to isolated validation state

It never accepts a client-supplied outcome, evidence_ref, adapter, or
hard-disqualifier list. It never grants trust, permissions, or role
certificates.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_slayer.planning.planner_certification import PLANNER_CERTIFICATION_POLICY_VERSION
from code_slayer.planning.qualification_evidence import read_planner_qualification_evidence
from code_slayer.security.evidence import (
    read_baseline_security_evidence,
)
from code_slayer.security.live_certification import (
    LiveOllamaRuntimeExpectation,
    certify_live_baseline_security,
    verify_ollama_runtime,
)
from code_slayer.security.live_planner_certification import (
    ROLE_LAYER_ELIGIBILITY_REASONS,
    certify_live_planner_role,
)
from code_slayer.store import db, location
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.certification_runs_repo import CertificationRunsRepo
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.production_eligibility import evaluate_production_eligibility
from code_slayer.workers.role_qualification import (
    ProductionRole,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import (
    runtime_profile_identity_from_config,
)

KIND_BASELINE = "baseline_security"
KIND_PLANNER = "planner_role"
ENVIRONMENT_VALIDATION = "VALIDATION"
ENVIRONMENT_PRODUCTION = "PRODUCTION"

PREFLIGHT_CHECKS = (
    "worker_registration",
    "worker_lifecycle_active",
    "runtime_profile",
    "ollama_root",
    "ollama_reachable",
    "ollama_version",
    "model_name",
    "digest_matches",
    "runtime_fingerprint",
    "isolated_certification_db",
    "isolated_evidence_directory",
    "production_state_unmodified",
)

PLANNER_PREFLIGHT_CHECKS = (
    "worker_registration",
    "worker_lifecycle_active",
    "runtime_profile",
    "role_target_configured",
    "policy_version_matches_canonical",
    "ollama_reachable",
    "ollama_version",
    "model_name",
    "digest_matches",
    "runtime_fingerprint",
    "production_baseline_security",
)


@dataclass(frozen=True)
class BaselineCertificationTarget:
    """Server-owned expected runtime for one worker. Never HTTP input."""

    worker_id: str
    expectation: LiveOllamaRuntimeExpectation


@dataclass(frozen=True)
class RoleEvaluationTarget:
    """Server-owned role/evaluation identity used only for eligibility
    diagnostics. Never a substitute for Baseline Security."""

    worker_id: str
    role: ProductionRole
    output_token_budget: int
    tool_choice_enforcement: str
    policy_version: str


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def _sourced(value, source: str, **extra) -> dict:
    data = {"value": value, "source": source}
    data.update(extra)
    return data


class CertificationService:
    def __init__(
        self,
        repo_id: str,
        worktree_id: str,
        *,
        state_root=None,
        targets: tuple[BaselineCertificationTarget, ...] = (),
        role_targets: tuple[RoleEvaluationTarget, ...] = (),
    ) -> None:
        self._repo_id = repo_id
        self._worktree_id = worktree_id
        self._state_root = state_root
        self._targets = {item.worker_id: item for item in targets}
        self._role_targets = {(item.worker_id, item.role): item for item in role_targets}
        self._production_conn: sqlite3.Connection | None = None
        self._validation_conn: sqlite3.Connection | None = None

    def close(self) -> None:
        if self._production_conn is not None:
            self._production_conn.close()
            self._production_conn = None
        if self._validation_conn is not None:
            self._validation_conn.close()
            self._validation_conn = None

    def production_paths(self) -> dict[str, Path]:
        return {
            "db": location.db_path(
                self._repo_id, self._worktree_id, override=self._state_root,
            ),
            "blobs": location.blobs_dir(
                self._repo_id, self._worktree_id, override=self._state_root,
            ),
        }

    def validation_paths(self) -> dict[str, Path]:
        return {
            "db": location.validation_certification_db_path(
                self._repo_id, self._worktree_id, override=self._state_root,
            ),
            "blobs": location.validation_certification_blobs_dir(
                self._repo_id, self._worktree_id, override=self._state_root,
            ),
        }

    def production_conn(self) -> sqlite3.Connection:
        if self._production_conn is None:
            path = self.production_paths()["db"]
            path.parent.mkdir(parents=True, exist_ok=True)
            self._production_conn = db.connect(path)
            db.migrate(self._production_conn)
        return self._production_conn

    def validation_conn(self) -> sqlite3.Connection:
        if self._validation_conn is None:
            location.ensure_validation_certification_dirs(
                self._repo_id, self._worktree_id, override=self._state_root,
            )
            path = self.validation_paths()["db"]
            self._validation_conn = db.connect(path)
            db.migrate(self._validation_conn)
        return self._validation_conn

    def target_for(self, worker_id: str) -> BaselineCertificationTarget | None:
        return self._targets.get(worker_id)

    def _ensure_validation_worker(self, worker_id: str) -> None:
        production = WorkersRepo(self.production_conn()).get(worker_id)
        if production is None:
            raise KeyError(worker_id)
        repo = WorkersRepo(self.validation_conn())
        if repo.get(worker_id) is None:
            repo.register(
                worker_id=production.worker_id,
                kind=production.kind,
                network_class=production.network_class,
            )

    def isolation_ok(self) -> tuple[bool, str]:
        production = self.production_paths()
        validation = self.validation_paths()
        if production["db"].resolve() == validation["db"].resolve():
            return False, "validation_db_is_production_db"
        if production["blobs"].resolve() == validation["blobs"].resolve():
            return False, "validation_blobs_are_production_blobs"
        return True, "isolated"

    def list_workers(self) -> list[dict]:
        rows = self.production_conn().execute(
            "SELECT worker_id FROM workers ORDER BY worker_id",
        ).fetchall()
        return [self.worker_summary(row["worker_id"]) for row in rows]

    def worker_summary(self, worker_id: str) -> dict:
        worker = WorkersRepo(self.production_conn()).get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
        target = self.target_for(worker_id)
        runtime = self._runtime_status(worker_id, target)
        baseline = self._baseline_status(worker_id, target)
        roles = {
            role.value: self._role_status(worker_id, role)
            for role in ProductionRole
        }
        eligibility = self._eligibility(worker_id, ProductionRole.PLANNER, target)
        # H.3: an ARCHIVED worker never shows an actionable READY
        # preflight/promotion here, even if a stale one exists from
        # before it was archived -- the WebUI's existing
        # certificationStartEnabled()/certificationPromoteEnabled()/
        # certificationPlannerStartEnabled() already gate purely off
        # these backend-authoritative booleans, so no new field is
        # needed to disable those actions. The actual mutation
        # boundaries (run_preflight()/start_baseline_run()/
        # start_planner_certification()/promote_to_production()) each
        # independently re-check lifecycle too -- this projection is a
        # display convenience, never itself an authority boundary.
        lifecycle_active = worker.lifecycle_state == "ACTIVE"
        ready = CertificationRunsRepo(self.validation_conn()).latest_ready(
            worker_id, kind=KIND_BASELINE,
        )
        promotion = self._promotion_availability(worker_id, target)
        planner_ready = CertificationRunsRepo(self.validation_conn()).latest_ready(
            worker_id, kind=KIND_PLANNER,
        )
        planner_active = CertificationRunsRepo(self.validation_conn()).active_for_worker(
            worker_id, kind=KIND_PLANNER,
        )
        planner_available = lifecycle_active and planner_ready is not None and not (
            planner_active is not None and planner_active.state in ("QUEUED", "RUNNING")
        )
        return {
            "worker_id": worker.worker_id,
            "kind": worker.kind,
            "network_class": worker.network_class,
            "environment": ENVIRONMENT_VALIDATION,
            "lifecycle_state": worker.lifecycle_state,
            "lifecycle_changed_at": worker.lifecycle_changed_at,
            "runtime": runtime,
            "baseline_security": baseline,
            "roles": roles,
            "production_eligibility": eligibility,
            "ready_for_certification": lifecycle_active and ready is not None,
            "promotion_available": lifecycle_active and promotion.available,
            "promotion_reason": promotion.reason if lifecycle_active else "worker_archived",
            "planner_ready_for_certification": planner_available,
            "future_actions": [
                {
                    "role": role.value,
                    "available": (
                        planner_available if role == ProductionRole.PLANNER else False
                    ),
                    "reason": (
                        "ready_for_certification"
                        if role == ProductionRole.PLANNER and planner_available
                        else "worker_archived"
                        if role == ProductionRole.PLANNER and not lifecycle_active
                        else "planner_preflight_required"
                        if role == ProductionRole.PLANNER
                        else "live_role_certification_unavailable"
                    ),
                }
                for role in ProductionRole
            ],
        }

    def worker_detail(self, worker_id: str) -> dict:
        summary = self.worker_summary(worker_id)
        target = self.target_for(worker_id)
        identity = None
        live = self._live_attestation(worker_id)
        if target is not None:
            profile = runtime_profile_identity_from_config(
                model_tag=target.expectation.model_tag,
                model_digest=target.expectation.model_digest,
                endpoint=target.expectation.openai_base_url,
                runtime_version=target.expectation.runtime_version,
                effective_context_tokens=target.expectation.effective_context_tokens,
                temperature=float(target.expectation.temperature),
                normalizer_id=target.expectation.normalizer_id,
                normalizer_version=target.expectation.normalizer_version,
            )
            identity = {
                "worker_id": worker_id,
                "provider_runtime": "ollama",
                "model_tag": _sourced(profile.model_tag, "CONFIG_BOUND"),
                "model_digest": _sourced(
                    live.get("digest") if live and live.get("digest_ok") else profile.model_digest,
                    "LIVE_ATTESTED" if live and live.get("digest_ok") else "CONFIG_BOUND",
                ),
                "runtime_identity_fingerprint": _sourced(
                    profile.runtime_identity_fingerprint,
                    "CONFIG_BOUND",
                ),
                "endpoint": _sourced(profile.endpoint, "CONFIG_BOUND"),
                "ollama_root": _sourced(
                    target.expectation.normalized_ollama_root, "CONFIG_BOUND",
                ),
                "runtime_version": _sourced(
                    live.get("version") if live and live.get("version_ok")
                    else profile.runtime_version,
                    "LIVE_ATTESTED" if live and live.get("version_ok") else "CONFIG_BOUND",
                ),
                "normalizer_id": _sourced(profile.normalizer_id, "CONFIG_BOUND"),
                "normalizer_version": _sourced(profile.normalizer_version, "CONFIG_BOUND"),
                "effective_context_tokens": _sourced(
                    profile.effective_context_tokens,
                    "CONFIG_BOUND",
                    measured_by_ollama=False,
                ),
                "temperature": _sourced(profile.temperature, "CONFIG_BOUND"),
            }
        last = self._latest_run_row(worker_id)
        last_preflight = None
        if last is not None:
            last_preflight = {
                "run_id": last.run_id,
                "state": last.state,
                "ready": last.state == "READY",
                "checks": self._preflight_checks_from(last),
            }
        planner_last = self._latest_run_row(worker_id, kind=KIND_PLANNER)
        planner_last_preflight = None
        if planner_last is not None:
            planner_last_preflight = {
                "run_id": planner_last.run_id,
                "state": planner_last.state,
                "ready": planner_last.state == "READY",
                "checks": self._preflight_checks_from(planner_last),
            }
        planner_active = CertificationRunsRepo(self.validation_conn()).active_for_worker(
            worker_id, kind=KIND_PLANNER,
        )
        return {
            **summary,
            "identity": identity,
            "history": self.history(worker_id),
            "last_preflight": last_preflight,
            "ready_for_certification": summary["ready_for_certification"],
            "planner_last_preflight": planner_last_preflight,
            "planner_active_run": (
                self._run_projection(planner_active)
                if planner_active is not None and planner_active.state in ("QUEUED", "RUNNING")
                else None
            ),
        }

    def _runtime_status(self, worker_id: str, target: BaselineCertificationTarget | None) -> dict:
        if target is None:
            return {"status": "UNKNOWN", "reason": "runtime_profile_not_configured"}
        live = self._live_attestation(worker_id)
        if live is None:
            return {"status": "UNKNOWN", "reason": "not_probed"}
        if not live["reachable"]:
            return {
                "status": "UNREACHABLE",
                "reason": live.get("reachable_detail") or "runtime_probe_unavailable",
            }
        if not live["digest_ok"]:
            return {"status": "MISMATCH", "reason": "runtime_model_digest_mismatch"}
        if not live["version_ok"]:
            return {"status": "MISMATCH", "reason": "runtime_version_mismatch"}
        if not live["fingerprint_ok"]:
            return {"status": "MISMATCH", "reason": "runtime_fingerprint_mismatch"}
        if not live["model_ok"]:
            return {"status": "MISMATCH", "reason": "runtime_model_missing"}
        return {"status": "VERIFIED", "reason": "preflight_live_attested"}

    def _baseline_status(self, worker_id: str, target: BaselineCertificationTarget | None) -> dict:
        certificates = BaselineSecurityCertificatesRepo(
            self.validation_conn(),
        ).list_for_worker(worker_id)
        if not certificates:
            return {
                "status": "NOT_CERTIFIED",
                "certificate_id": None,
                "outcome": None,
                "environment": ENVIRONMENT_VALIDATION,
            }
        latest = certificates[0]
        if target is not None:
            profile = runtime_profile_identity_from_config(
                model_tag=target.expectation.model_tag,
                model_digest=target.expectation.model_digest,
                endpoint=target.expectation.openai_base_url,
                runtime_version=target.expectation.runtime_version,
                effective_context_tokens=target.expectation.effective_context_tokens,
                temperature=float(target.expectation.temperature),
                normalizer_id=target.expectation.normalizer_id,
                normalizer_version=target.expectation.normalizer_version,
            )
            matching = [
                item for item in certificates
                if item.runtime_identity_fingerprint == profile.runtime_identity_fingerprint
            ]
            if matching:
                latest = matching[0]
        status = {
            "PASS": "CERTIFIED",
            "FAIL": "FAILED",
            "HARD_DISQUALIFIED": "FAILED",
        }.get(latest.outcome, "NOT_CERTIFIED")
        return {
            "status": status,
            "certificate_id": latest.certificate_id,
            "outcome": latest.outcome,
            "reason": latest.reason,
            "evidence_ref": latest.evidence_ref,
            "environment": ENVIRONMENT_VALIDATION,
        }

    def _role_status(self, worker_id: str, role: ProductionRole) -> dict:
        rows = RoleCertificatesRepo(self.production_conn()).list_for_worker_role(
            worker_id, role.value,
        )
        if not rows:
            return {"status": "NOT_CERTIFIED", "certificate_id": None, "outcome": None}
        latest = rows[0]
        return {
            "status": "CERTIFIED" if latest.outcome == "PASS" else "NOT_CERTIFIED",
            "certificate_id": latest.certificate_id,
            "outcome": latest.outcome,
            "policy_version": latest.policy_version,
        }

    def _eligibility(
        self,
        worker_id: str,
        role: ProductionRole,
        target: BaselineCertificationTarget | None,
    ) -> dict:
        role_target = self._role_targets.get((worker_id, role))
        if target is None:
            return {
                "eligible": False,
                "reason": "runtime_profile_not_configured",
                "source": "configuration",
            }
        if role_target is None:
            return {
                "eligible": False,
                "reason": "role_evaluation_not_configured",
                "source": "configuration",
            }
        profile = runtime_profile_identity_from_config(
            model_tag=target.expectation.model_tag,
            model_digest=target.expectation.model_digest,
            endpoint=target.expectation.openai_base_url,
            runtime_version=target.expectation.runtime_version,
            effective_context_tokens=target.expectation.effective_context_tokens,
            temperature=float(target.expectation.temperature),
            normalizer_id=target.expectation.normalizer_id,
            normalizer_version=target.expectation.normalizer_version,
        )
        evaluation = role_evaluation_identity_from_config(
            role=role_target.role,
            runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
            output_token_budget=role_target.output_token_budget,
            tool_choice_enforcement=role_target.tool_choice_enforcement,
            policy_version=role_target.policy_version,
        )
        decision = evaluate_production_eligibility(
            self.production_conn(),
            worker_id=worker_id,
            role=role,
            runtime_profile=profile,
            role_evaluation=evaluation,
            expected_role_policy_version=role_target.policy_version,
        )
        return {
            "eligible": decision.eligible,
            "reason": decision.reason,
            "source": "evaluate_production_eligibility",
            "security_certificate_id": decision.security_certificate_id,
            "role_certificate_id": decision.role_certificate_id,
        }

    def _promotion_availability(
        self, worker_id: str, target: BaselineCertificationTarget | None,
    ):
        """H.1: the ONE backend-authoritative source for whether the
        WebUI promotion action should be offered. Never a live Ollama
        probe (GET routes in this codebase never probe live -- see
        `_runtime_status`'s own precedent); this is a durable-state-only
        projection, advisory the same way `ready_for_certification`
        already is. `promote_to_production()` independently re-verifies
        everything live and remains the only authoritative decision --
        a `True` here can still be denied there if live state changed
        in between."""
        from code_slayer.security.production_promotion import (
            PromotionAvailability,
            describe_promotion_availability,
        )

        if target is None:
            return PromotionAvailability(False, "runtime_profile_not_configured")
        return describe_promotion_availability(
            self.validation_conn(),
            self.production_conn(),
            worker_id=worker_id,
            expected=target.expectation,
        )

    def history(self, worker_id: str) -> dict:
        self._ensure_validation_db()
        runs = [
            self._run_projection(item)
            for item in CertificationRunsRepo(self.validation_conn()).list_for_worker(worker_id)
        ]
        certificates = [
            {
                "kind": "CERTIFICATE",
                "certificate_id": item.certificate_id,
                "outcome": item.outcome,
                "reason": item.reason,
                "evidence_ref": item.evidence_ref,
                "issued_at": item.issued_at,
                "environment": ENVIRONMENT_VALIDATION,
            }
            for item in BaselineSecurityCertificatesRepo(
                self.validation_conn(),
            ).list_for_worker(worker_id)
        ]
        production_certificates = [
            {
                "kind": "CERTIFICATE",
                "certificate_id": item.certificate_id,
                "outcome": item.outcome,
                "reason": item.reason,
                "evidence_ref": item.evidence_ref,
                "issued_at": item.issued_at,
                "environment": "PRODUCTION",
            }
            for item in BaselineSecurityCertificatesRepo(
                self.production_conn(),
            ).list_for_worker(worker_id)
        ]
        return {
            "runs": runs,
            "validation_certificates": certificates,
            "production_certificates": production_certificates,
        }

    def _ensure_validation_db(self) -> None:
        self.validation_conn()

    def _latest_run_row(self, worker_id: str, *, kind: str = KIND_BASELINE):
        rows = CertificationRunsRepo(self.validation_conn()).list_for_worker(
            worker_id, limit=1, kind=kind,
        )
        return rows[0] if rows else None

    def _preflight_checks_from(self, row) -> list[dict]:
        if row is None:
            return []
        try:
            checks = json.loads(row.preflight_json)
        except json.JSONDecodeError:
            return []
        if not isinstance(checks, list):
            return []
        return [item for item in checks if isinstance(item, dict)]

    def _live_attestation(self, worker_id: str) -> dict | None:
        named = {
            item.get("name"): item
            for item in self._preflight_checks_from(self._latest_run_row(worker_id))
        }
        if not named:
            return None
        reachable = named.get("ollama_reachable") or {}
        digest = named.get("digest_matches") or {}
        version = named.get("ollama_version") or {}
        fingerprint = named.get("runtime_fingerprint") or {}
        model = named.get("model_name") or {}
        return {
            "reachable": bool(reachable.get("ok")),
            "reachable_detail": reachable.get("detail") or "",
            "digest_ok": bool(digest.get("ok")),
            "digest": digest.get("detail") or None,
            "version_ok": bool(version.get("ok")),
            "version": version.get("detail") or None,
            "fingerprint_ok": bool(fingerprint.get("ok")),
            "fingerprint": fingerprint.get("detail") or None,
            "model_ok": bool(model.get("ok")),
        }

    def run_preflight(self, worker_id: str) -> dict:
        checks: list[dict] = []
        worker = WorkersRepo(self.production_conn()).get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
        active = CertificationRunsRepo(self.validation_conn()).active_for_worker(
            worker_id, kind=KIND_BASELINE,
        )
        if active is not None and active.state in ("QUEUED", "RUNNING"):
            raise CertificationConflict("certification_already_in_progress", active.run_id)
        checks.append(_check("worker_registration", True))
        lifecycle_active = worker.lifecycle_state == "ACTIVE"
        checks.append(_check(
            "worker_lifecycle_active", lifecycle_active,
            "" if lifecycle_active else "worker_archived",
        ))
        target = self.target_for(worker_id)
        checks.append(_check(
            "runtime_profile", target is not None,
            "" if target is not None else "runtime_profile_not_configured",
        ))
        isolated, isolation_reason = self.isolation_ok()
        checks.append(_check("isolated_certification_db", isolated, isolation_reason))
        checks.append(_check("isolated_evidence_directory", isolated, isolation_reason))
        production_before = self._production_fingerprint()
        checks.append(_check(
            "production_state_unmodified", True, "validation_workflow_does_not_select_production",
        ))

        # H.3: an ARCHIVED worker never reaches the live-probe block
        # below -- `worker_lifecycle_active` (and, when configured,
        # `runtime_profile`) are the only checks it can possibly
        # satisfy; every network-only check synthesizes `ok=False`/
        # `"not_evaluated"` via `_ensure_named()` below, exactly like
        # the existing `target is None` case already does. No live
        # Ollama/model contact happens for an archived worker.
        expectation = None
        if target is not None and lifecycle_active:
            expectation = target.expectation
            checks.append(_check("ollama_root", True, expectation.normalized_ollama_root))
            try:
                profile = runtime_profile_identity_from_config(
                    model_tag=expectation.model_tag,
                    model_digest=expectation.model_digest,
                    endpoint=expectation.openai_base_url,
                    runtime_version=expectation.runtime_version,
                    effective_context_tokens=expectation.effective_context_tokens,
                    temperature=float(expectation.temperature),
                    normalizer_id=expectation.normalizer_id,
                    normalizer_version=expectation.normalizer_version,
                )
                fingerprint_ok = (
                    profile.runtime_identity_fingerprint
                    == expectation.expected_runtime_identity_fingerprint
                )
                checks.append(_check(
                    "runtime_fingerprint", fingerprint_ok,
                    profile.runtime_identity_fingerprint,
                ))
            except (TypeError, ValueError) as exc:
                checks.append(_check("runtime_fingerprint", False, str(exc)))
            try:
                version, digest = verify_ollama_runtime(expectation)
                checks.append(_check("ollama_reachable", True))
                checks.append(_check("ollama_version", True, version))
                checks.append(_check("model_name", True, expectation.model_tag))
                checks.append(_check("digest_matches", True, digest))
            except ValueError as exc:
                reason = str(exc) or "runtime_probe_unavailable"
                reachable = reason not in {
                    "runtime_probe_unavailable",
                    "runtime_probe_redirect",
                    "runtime_probe_response_too_large",
                    "runtime_probe_malformed_json",
                }
                checks.append(_check("ollama_reachable", reachable, reason))
                checks.append(_check(
                    "ollama_version", reason != "runtime_version_mismatch", reason,
                ))
                checks.append(_check(
                    "model_name",
                    reason not in {"runtime_model_missing", "runtime_model_duplicate"},
                    reason,
                ))
                checks.append(_check(
                    "digest_matches", reason != "runtime_model_digest_mismatch", reason,
                ))

        named = {item["name"]: item for item in checks}
        ordered = [_ensure_named(named, name) for name in PREFLIGHT_CHECKS]
        ready = all(item["ok"] for item in ordered)
        self._ensure_validation_worker(worker_id)
        state = "READY" if ready else "INCOMPLETE"
        reason = "ready_for_certification" if ready else "preflight_blocked"
        now = utcnow_iso()
        with transaction(self.validation_conn()):
            runs = CertificationRunsRepo(self.validation_conn())
            runs.supersede_ready_in_transaction(worker_id, kind=KIND_BASELINE, now=now)
            row = runs.create_in_transaction(
                run_id=uuid.uuid4().hex,
                worker_id=worker_id,
                kind=KIND_BASELINE,
                environment=ENVIRONMENT_VALIDATION,
                state=state,
                created_at=now,
                preflight_json=json.dumps(ordered),
                reason=reason,
                expected_runtime_identity_fingerprint=(
                    expectation.expected_runtime_identity_fingerprint if expectation else None
                ),
                model_tag=expectation.model_tag if expectation else None,
                model_digest=expectation.model_digest if expectation else None,
                ollama_root=expectation.normalized_ollama_root if expectation else None,
            )
        production_after = self._production_fingerprint()
        if production_before != production_after:
            raise RuntimeError("production_state_modified_by_preflight")
        projection = self._run_projection(row)
        projection["ready"] = ready
        projection["checks"] = ordered
        return projection

    def _production_fingerprint(self) -> tuple:
        conn = self.production_conn()
        baseline = conn.execute(
            "SELECT count(*) AS c FROM worker_baseline_security_certificates",
        ).fetchone()["c"]
        roles = conn.execute("SELECT count(*) AS c FROM worker_role_certificates").fetchone()["c"]
        trust = conn.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"]
        grants = conn.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"]
        return (baseline, roles, trust, grants)

    def start_baseline_run(self, worker_id: str) -> dict:
        worker = WorkersRepo(self.production_conn()).get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
        # H.3: re-checked fresh here, independent of whatever
        # `worker_lifecycle_active` said in a possibly-stale READY
        # preflight snapshot -- a worker can be archived after a READY
        # preflight already exists, and this boundary must not trust
        # that snapshot for lifecycle.
        if worker.lifecycle_state != "ACTIVE":
            raise CertificationBlocked("worker_archived")
        self._ensure_validation_worker(worker_id)
        repo = CertificationRunsRepo(self.validation_conn())
        now = utcnow_iso()
        with transaction(self.validation_conn()):
            active = repo.active_for_worker(worker_id, kind=KIND_BASELINE)
            if active is not None and active.state in ("QUEUED", "RUNNING"):
                raise CertificationConflict("certification_already_in_progress", active.run_id)
            ready = repo.latest_ready(worker_id, kind=KIND_BASELINE)
            if ready is None or ready.state != "READY":
                raise CertificationBlocked("preflight_required")
            try:
                checks = json.loads(ready.preflight_json)
            except json.JSONDecodeError:
                checks = []
            if not checks or not all(item.get("ok") for item in checks):
                raise CertificationBlocked("preflight_blocked")
            try:
                row = repo.queue_in_transaction(ready.run_id, now=now)
            except KeyError as exc:
                active_now = repo.active_for_worker(worker_id, kind=KIND_BASELINE)
                if active_now is not None and active_now.state in ("QUEUED", "RUNNING"):
                    raise CertificationConflict(
                        "certification_already_in_progress", active_now.run_id,
                    ) from exc
                raise CertificationBlocked("preflight_required") from exc
        return self._run_projection(row)

    def _planner_baseline_security_check(
        self, worker_id: str, expectation, role_target, fingerprint: str | None,
    ) -> dict:
        """H.2: the `production_baseline_security` preflight check --
        consults the EXISTING `evaluate_production_eligibility()`
        evaluator rather than a fourth reimplementation of certificate
        matching. See `security.live_planner_certification`'s own
        docstring (pre-condition 5) for the exact reason vocabulary."""
        if expectation is None or role_target is None or fingerprint is None:
            return _check(
                "production_baseline_security", False, "runtime_profile_not_configured",
            )
        try:
            profile = runtime_profile_identity_from_config(
                model_tag=expectation.model_tag,
                model_digest=expectation.model_digest,
                endpoint=expectation.openai_base_url,
                runtime_version=expectation.runtime_version,
                effective_context_tokens=expectation.effective_context_tokens,
                temperature=float(expectation.temperature),
                normalizer_id=expectation.normalizer_id,
                normalizer_version=expectation.normalizer_version,
            )
            role_evaluation = role_evaluation_identity_from_config(
                role=ProductionRole.PLANNER,
                runtime_identity_fingerprint=fingerprint,
                output_token_budget=role_target.output_token_budget,
                tool_choice_enforcement=role_target.tool_choice_enforcement,
                policy_version=role_target.policy_version,
            )
        except (TypeError, ValueError) as exc:
            return _check("production_baseline_security", False, str(exc))
        decision = evaluate_production_eligibility(
            self.production_conn(),
            worker_id=worker_id,
            role=ProductionRole.PLANNER,
            runtime_profile=profile,
            role_evaluation=role_evaluation,
            expected_role_policy_version=role_target.policy_version,
        )
        ok = decision.eligible or decision.reason in ROLE_LAYER_ELIGIBILITY_REASONS
        return _check("production_baseline_security", ok, decision.reason)

    def run_planner_preflight(self, worker_id: str) -> dict:
        checks: list[dict] = []
        worker = WorkersRepo(self.production_conn()).get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
        active = CertificationRunsRepo(self.validation_conn()).active_for_worker(
            worker_id, kind=KIND_PLANNER,
        )
        if active is not None and active.state in ("QUEUED", "RUNNING"):
            raise CertificationConflict("certification_already_in_progress", active.run_id)
        checks.append(_check("worker_registration", True))
        lifecycle_active = worker.lifecycle_state == "ACTIVE"
        checks.append(_check(
            "worker_lifecycle_active", lifecycle_active,
            "" if lifecycle_active else "worker_archived",
        ))
        target = self.target_for(worker_id)
        checks.append(_check(
            "runtime_profile", target is not None,
            "" if target is not None else "runtime_profile_not_configured",
        ))
        role_target = self._role_targets.get((worker_id, ProductionRole.PLANNER))
        checks.append(_check(
            "role_target_configured", role_target is not None,
            "" if role_target is not None else "role_evaluation_not_configured",
        ))
        # H.2 fix: the configured policy version must equal EXACTLY the
        # one `certify_planner_from_qualification()` itself uses
        # (`planning.planner_certification.
        # PLANNER_CERTIFICATION_POLICY_VERSION`) -- checked here, before
        # any network I/O, so drifted persistent config can never reach
        # `start_planner_certification()` at all. See `security.
        # live_planner_certification`'s own module docstring ("One
        # authoritative policy identity") for why this must never
        # diverge.
        policy_version_ok = (
            role_target is not None
            and role_target.policy_version == PLANNER_CERTIFICATION_POLICY_VERSION
        )
        checks.append(_check(
            "policy_version_matches_canonical", policy_version_ok,
            "" if policy_version_ok else "planner_certification_policy_version_mismatch",
        ))
        production_before = self._production_fingerprint()

        # H.3: same as run_preflight() -- an ARCHIVED worker never
        # reaches this live-probe block.
        expectation = None
        fingerprint = None
        if target is not None and lifecycle_active:
            expectation = target.expectation
            try:
                profile = runtime_profile_identity_from_config(
                    model_tag=expectation.model_tag,
                    model_digest=expectation.model_digest,
                    endpoint=expectation.openai_base_url,
                    runtime_version=expectation.runtime_version,
                    effective_context_tokens=expectation.effective_context_tokens,
                    temperature=float(expectation.temperature),
                    normalizer_id=expectation.normalizer_id,
                    normalizer_version=expectation.normalizer_version,
                )
                fingerprint = profile.runtime_identity_fingerprint
                fingerprint_ok = fingerprint == expectation.expected_runtime_identity_fingerprint
                checks.append(_check("runtime_fingerprint", fingerprint_ok, fingerprint))
            except (TypeError, ValueError) as exc:
                checks.append(_check("runtime_fingerprint", False, str(exc)))
            try:
                version, digest = verify_ollama_runtime(expectation)
                checks.append(_check("ollama_reachable", True))
                checks.append(_check("ollama_version", True, version))
                checks.append(_check("model_name", True, expectation.model_tag))
                checks.append(_check("digest_matches", True, digest))
            except ValueError as exc:
                reason = str(exc) or "runtime_probe_unavailable"
                reachable = reason not in {
                    "runtime_probe_unavailable",
                    "runtime_probe_redirect",
                    "runtime_probe_response_too_large",
                    "runtime_probe_malformed_json",
                }
                checks.append(_check("ollama_reachable", reachable, reason))
                checks.append(_check(
                    "ollama_version", reason != "runtime_version_mismatch", reason,
                ))
                checks.append(_check(
                    "model_name",
                    reason not in {"runtime_model_missing", "runtime_model_duplicate"},
                    reason,
                ))
                checks.append(_check(
                    "digest_matches", reason != "runtime_model_digest_mismatch", reason,
                ))

        checks.append(
            self._planner_baseline_security_check(worker_id, expectation, role_target, fingerprint),
        )

        named = {item["name"]: item for item in checks}
        ordered = [_ensure_named(named, name) for name in PLANNER_PREFLIGHT_CHECKS]
        ready = all(item["ok"] for item in ordered)
        self._ensure_validation_worker(worker_id)
        state = "READY" if ready else "INCOMPLETE"
        reason = "ready_for_certification" if ready else "preflight_blocked"
        now = utcnow_iso()
        with transaction(self.validation_conn()):
            runs = CertificationRunsRepo(self.validation_conn())
            runs.supersede_ready_in_transaction(worker_id, kind=KIND_PLANNER, now=now)
            row = runs.create_in_transaction(
                run_id=uuid.uuid4().hex,
                worker_id=worker_id,
                kind=KIND_PLANNER,
                environment=ENVIRONMENT_VALIDATION,
                state=state,
                created_at=now,
                preflight_json=json.dumps(ordered),
                reason=reason,
                expected_runtime_identity_fingerprint=(
                    expectation.expected_runtime_identity_fingerprint if expectation else None
                ),
                model_tag=expectation.model_tag if expectation else None,
                model_digest=expectation.model_digest if expectation else None,
                ollama_root=expectation.normalized_ollama_root if expectation else None,
            )
        production_after = self._production_fingerprint()
        if production_before != production_after:
            raise RuntimeError("production_state_modified_by_preflight")
        projection = self._run_projection(row)
        projection["ready"] = ready
        projection["checks"] = ordered
        return projection

    def start_planner_certification(self, worker_id: str) -> dict:
        worker = WorkersRepo(self.production_conn()).get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
        # H.3: re-checked fresh -- see start_baseline_run()'s own
        # comment for why this must not rely on the READY preflight's
        # own possibly-stale `worker_lifecycle_active` snapshot.
        if worker.lifecycle_state != "ACTIVE":
            raise CertificationBlocked("worker_archived")
        self._ensure_validation_worker(worker_id)
        repo = CertificationRunsRepo(self.validation_conn())
        now = utcnow_iso()
        with transaction(self.validation_conn()):
            active = repo.active_for_worker(worker_id, kind=KIND_PLANNER)
            if active is not None and active.state in ("QUEUED", "RUNNING"):
                raise CertificationConflict("certification_already_in_progress", active.run_id)
            ready = repo.latest_ready(worker_id, kind=KIND_PLANNER)
            if ready is None or ready.state != "READY":
                raise CertificationBlocked("preflight_required")
            try:
                checks = json.loads(ready.preflight_json)
            except json.JSONDecodeError:
                checks = []
            if not checks or not all(item.get("ok") for item in checks):
                raise CertificationBlocked("preflight_blocked")
            try:
                row = repo.queue_in_transaction(ready.run_id, now=now)
            except KeyError as exc:
                active_now = repo.active_for_worker(worker_id, kind=KIND_PLANNER)
                if active_now is not None and active_now.state in ("QUEUED", "RUNNING"):
                    raise CertificationConflict(
                        "certification_already_in_progress", active_now.run_id,
                    ) from exc
                raise CertificationBlocked("preflight_required") from exc
        return self._run_projection(row)

    def promote_to_production(self, worker_id: str) -> dict:
        """H.1: re-verify and durably carry forward one `PASS` VALIDATION
        Baseline Security certificate into PRODUCTION. See `code_slayer.
        security.production_promotion` for the complete fail-closed check
        list. Grants no trust, permission, or role certificate; never
        touches `worker_trust_events`/`permission_grants`/any role
        certificate table."""
        from code_slayer.security.production_promotion import (
            promote_baseline_security_to_production,
        )

        worker = WorkersRepo(self.production_conn()).get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
        # H.3: re-checked fresh at the promotion boundary too -- a
        # worker can be archived between a VALIDATION PASS and the
        # operator clicking promote.
        if worker.lifecycle_state != "ACTIVE":
            raise CertificationBlocked("worker_archived")
        target = self.target_for(worker_id)
        if target is None:
            raise CertificationBlocked("runtime_profile_not_configured")
        # A worker that is registered in PRODUCTION but has never had a
        # preflight/certification run started has no VALIDATION `workers`
        # row yet (see `_ensure_validation_worker`). Establish it here too,
        # so "no certificate was ever recorded" reports as
        # `no_validation_certificate`, not a misleading `unknown_worker`.
        self._ensure_validation_worker(worker_id)
        result = promote_baseline_security_to_production(
            self.validation_conn(),
            self.production_conn(),
            worker_id=worker_id,
            validation_blobs_dir=self.validation_paths()["blobs"],
            production_blobs_dir=self.production_paths()["blobs"],
            expected=target.expectation,
        )
        if not result.ok:
            raise CertificationBlocked(result.reason)
        return {
            "worker_id": worker_id,
            "environment": ENVIRONMENT_PRODUCTION,
            "reason": result.reason,
            "validation_certificate_id": result.validation_certificate_id,
            "production_certificate_id": result.production_certificate_id,
            "runtime_identity_fingerprint": result.runtime_identity_fingerprint,
            "evidence_ref": result.evidence_ref,
        }

    def get_run(self, run_id: str) -> dict:
        row = CertificationRunsRepo(self.validation_conn()).get_or_none(run_id)
        if row is None:
            raise KeyError(run_id)
        return self._run_projection(row)

    def run_evidence(self, run_id: str) -> dict:
        row = CertificationRunsRepo(self.validation_conn()).get_or_none(run_id)
        if row is None:
            raise KeyError(run_id)
        expected = row.expected_runtime_identity_fingerprint
        if row.kind == KIND_PLANNER:
            if not row.evidence_ref:
                raise CertificationBlocked("missing_durable_qualification_evidence")
            # Planner evidence is written directly to PRODUCTION (H.2 has
            # no VALIDATION/PRODUCTION split for role certificates) --
            # only the run/preflight bookkeeping row lives in VALIDATION.
            document = read_planner_qualification_evidence(
                self.production_conn(),
                self.production_paths()["blobs"],
                row.evidence_ref,
                expected_runtime_identity_fingerprint=expected,
            )
            environment = ENVIRONMENT_PRODUCTION
        else:
            if not row.evidence_ref:
                raise CertificationBlocked("missing_durable_security_evidence")
            document = read_baseline_security_evidence(
                self.validation_conn(),
                self.validation_paths()["blobs"],
                row.evidence_ref,
                expected_runtime_identity_fingerprint=expected,
            )
            environment = ENVIRONMENT_VALIDATION
        return {
            "run_id": run_id,
            "environment": environment,
            "evidence_ref": row.evidence_ref,
            "document": document,
        }

    def claimable_run_ids(self) -> list[str]:
        return CertificationRunsRepo(self.validation_conn()).claimable_ids()

    def claim_run(self, run_id: str) -> object | None:
        now = utcnow_iso()
        with transaction(self.validation_conn()):
            return CertificationRunsRepo(self.validation_conn()).claim_in_transaction(
                run_id,
                owner_pid=os.getpid(),
                owner_pid_started_at=None,
                now=now,
            )

    def execute_claimed_run(self, claimed) -> None:
        if claimed.kind == KIND_PLANNER:
            self._execute_claimed_planner_run(claimed)
            return
        target = self.target_for(claimed.worker_id)
        repo = CertificationRunsRepo(self.validation_conn())
        if target is None:
            with transaction(self.validation_conn()):
                repo.finish_in_transaction(
                    claimed.run_id,
                    state="INCOMPLETE",
                    expected_generation=claimed.owner_generation,
                    now=utcnow_iso(),
                    reason="runtime_profile_not_configured",
                )
            return
        # H.3: re-read PRODUCTION lifecycle (the VALIDATION `workers`
        # mirror is never authoritative for it -- see `workers.
        # lifecycle`'s own docstring) immediately before any model
        # call, closing the queued-before-archive race: if the worker
        # was archived after this run was queued but before it was
        # claimed here, refuse now -- no model call, no certificate.
        production_worker = WorkersRepo(self.production_conn()).get(claimed.worker_id)
        if production_worker is None or production_worker.lifecycle_state != "ACTIVE":
            with transaction(self.validation_conn()):
                repo.finish_in_transaction(
                    claimed.run_id,
                    state="INCOMPLETE",
                    expected_generation=claimed.owner_generation,
                    now=utcnow_iso(),
                    reason="worker_archived",
                )
            return
        result = certify_live_baseline_security(
            self.validation_conn(),
            worker_id=claimed.worker_id,
            blobs_dir=self.validation_paths()["blobs"],
            expected=target.expectation,
        )
        if result.ok and result.outcome is not None:
            state = result.outcome.value
            reason = result.reason
        else:
            state = "INCOMPLETE"
            reason = result.reason
        hard = json.dumps([item.value for item in result.hard_disqualifiers])
        with transaction(self.validation_conn()):
            repo.finish_in_transaction(
                claimed.run_id,
                state=state,
                expected_generation=claimed.owner_generation,
                now=utcnow_iso(),
                reason=reason,
                certificate_id=result.certificate_id,
                evidence_ref=result.evaluation_evidence_ref,
                hard_disqualifiers_json=hard,
            )

    def _execute_claimed_planner_run(self, claimed) -> None:
        """H.2: unlike Baseline Security, the certificate write (and its
        evidence) lands directly in PRODUCTION -- only this run/
        bookkeeping row lives in VALIDATION. See `security.
        live_planner_certification`'s own docstring for why."""
        target = self.target_for(claimed.worker_id)
        role_target = self._role_targets.get((claimed.worker_id, ProductionRole.PLANNER))
        repo = CertificationRunsRepo(self.validation_conn())
        if target is None or role_target is None:
            with transaction(self.validation_conn()):
                repo.finish_in_transaction(
                    claimed.run_id,
                    state="INCOMPLETE",
                    expected_generation=claimed.owner_generation,
                    now=utcnow_iso(),
                    reason=(
                        "runtime_profile_not_configured"
                        if target is None
                        else "role_evaluation_not_configured"
                    ),
                )
            return
        # H.3: same execution-time recheck as execute_claimed_run()
        # above -- see that method's own comment.
        production_worker = WorkersRepo(self.production_conn()).get(claimed.worker_id)
        if production_worker is None or production_worker.lifecycle_state != "ACTIVE":
            with transaction(self.validation_conn()):
                repo.finish_in_transaction(
                    claimed.run_id,
                    state="INCOMPLETE",
                    expected_generation=claimed.owner_generation,
                    now=utcnow_iso(),
                    reason="worker_archived",
                )
            return
        result = certify_live_planner_role(
            self.production_conn(),
            worker_id=claimed.worker_id,
            blobs_dir=self.production_paths()["blobs"],
            expected=target.expectation,
            output_token_budget=role_target.output_token_budget,
            tool_choice_enforcement=role_target.tool_choice_enforcement,
            policy_version=role_target.policy_version,
        )
        if result.ok and result.outcome is not None:
            state = result.outcome.value
            reason = result.reason
        else:
            state = "INCOMPLETE"
            reason = result.reason
        with transaction(self.validation_conn()):
            repo.finish_in_transaction(
                claimed.run_id,
                state=state,
                expected_generation=claimed.owner_generation,
                now=utcnow_iso(),
                reason=reason,
                certificate_id=result.certificate_id,
                evidence_ref=result.evidence_ref,
            )

    def fail_claimed_run(self, claimed, reason: str) -> None:
        with transaction(self.validation_conn()):
            CertificationRunsRepo(self.validation_conn()).finish_in_transaction(
                claimed.run_id,
                state="INCOMPLETE",
                expected_generation=claimed.owner_generation,
                now=utcnow_iso(),
                reason=reason,
            )

    def _run_projection(self, row) -> dict:
        duration = None
        if row.started_at and row.finished_at:
            duration = {"started_at": row.started_at, "finished_at": row.finished_at}
        progress = _progress_for(row.state)
        try:
            checks = json.loads(row.preflight_json)
        except json.JSONDecodeError:
            checks = []
        try:
            hard = json.loads(row.hard_disqualifiers_json)
        except json.JSONDecodeError:
            hard = []
        return {
            "run_id": row.run_id,
            "worker_id": row.worker_id,
            "kind": row.kind,
            "environment": row.environment,
            "state": row.state,
            "reason": row.reason,
            "checks": checks,
            "model_tag": row.model_tag,
            "model_digest": row.model_digest,
            "ollama_root": row.ollama_root,
            "runtime_identity_fingerprint": row.expected_runtime_identity_fingerprint,
            "certificate_id": row.certificate_id,
            "evidence_ref": row.evidence_ref,
            "hard_disqualifiers": hard,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "duration": duration,
            "progress": progress,
            "has_certificate": row.certificate_id is not None,
        }


def _progress_for(state: str) -> list[dict]:
    stages = [
        "runtime_verification",
        "security_evaluation",
        "evidence_verification",
        "post_runtime_verification",
        "certificate_recording",
    ]
    complete = {
        "PREFLIGHT": [],
        "READY": ["runtime_verification"],
        "QUEUED": ["runtime_verification"],
        "RUNNING": ["runtime_verification", "security_evaluation"],
        "PASS": stages,
        "FAIL": stages,
        "HARD_DISQUALIFIED": stages,
        "INCOMPLETE": ["runtime_verification"],
    }
    done = complete.get(state, [])
    running = {
        "RUNNING": "security_evaluation",
        "QUEUED": "security_evaluation",
    }.get(state)
    out = []
    for name in stages:
        if name in done and name != running:
            status = "done"
        elif name == running:
            status = "running"
        else:
            status = "pending"
        if state == "INCOMPLETE" and name != "runtime_verification":
            status = "pending"
        out.append({"name": name, "status": status})
    return out


def _ensure_named(named: dict, name: str) -> dict:
    if name in named:
        return named[name]
    return _check(name, False, "not_evaluated")


class CertificationConflict(Exception):
    def __init__(self, code: str, run_id: str) -> None:
        self.code = code
        self.run_id = run_id


class CertificationBlocked(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
