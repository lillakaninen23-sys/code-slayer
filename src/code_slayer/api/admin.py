"""WebUI/CLI administration facade. Browser is a control surface only."""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from code_slayer import __version__
from code_slayer.admin.process import ProcessError
from code_slayer.admin.runtime import attest_worker
from code_slayer.admin.service import restart_service, service_status
from code_slayer.admin.tailscale import disable_serve, enable_serve
from code_slayer.admin.tailscale import status as tailscale_status
from code_slayer.admin.updates import apply_update, check_for_update
from code_slayer.api.service import APIError
from code_slayer.config.schema import (
    ConfigError,
    OllamaServerConfig,
    WorkerRuntimeConfig,
)
from code_slayer.security.live_certification import probe_ollama_inventory


class AdminFacade:
    def __init__(self, application) -> None:
        self._app = application

    def system_status(self) -> dict:
        status = service_status()
        process = self._app.process_identity
        checkout = self._app.current_checkout_identity()
        checkout_head_source = "OBSERVED" if checkout.commit is not None else "UNVERIFIED"
        checkout_state_source = (
            "OBSERVED" if checkout.dirty is not None else "UNVERIFIED"
        )
        if (
            process.commit is None
            or checkout.commit is None
            or process.dirty is None
            or checkout.dirty is None
        ):
            deployment_status = "UNVERIFIED"
            deployment_complete = False
        elif process.commit != checkout.commit:
            deployment_status = "MISMATCH"
            deployment_complete = False
        elif process.commit_source != "VERIFIED" or checkout.dirty:
            deployment_status = "DIRTY"
            deployment_complete = False
        else:
            deployment_status = "VERIFIED"
            deployment_complete = True
        cfg = self._app.persistent_config()
        return {
            "service": {
                "state": status.state,
                "running": status.active,
                "unit": status.unit,
                "source": status.source,
                "version": __version__,
                "process_commit": process.commit,
                "process_commit_source": process.commit_source,
                "process_source_dirty": process.dirty,
                "process_source_state": process.state,
                "running_commit": process.commit,
                "running_commit_source": process.commit_source,
                "checkout_head": checkout.commit,
                "checkout_head_source": checkout_head_source,
                "checkout_source_dirty": checkout.dirty,
                "checkout_source_state": checkout.state,
                "checkout_source_state_source": checkout_state_source,
                "deployment_status": deployment_status,
                "deployment_complete": deployment_complete,
                "uptime_seconds": int(time.monotonic() - self._app._started_at),
            },
            "network": {
                "local_url": f"http://{cfg.server.host}:{cfg.server.port}",
                "bind_host": cfg.server.host,
                "bind_port": cfg.server.port,
            },
            "health": self._health_fragment(),
        }

    def _health_fragment(self) -> dict:
        try:
            with self._app.reads() as reads:
                project = reads.project()
            return {
                "status": "ok",
                "schema_version": project["schema_version"],
                "source": "VERIFIED",
            }
        except Exception:
            return {"status": "unavailable", "schema_version": None, "source": "UNVERIFIED"}

    def runtime_overview(self, *, probe: bool = False) -> dict:
        cfg = self._app.persistent_config()
        servers = []
        for server in cfg.ollama_servers:
            entry = {
                "id": server.server_id,
                "origin": server.origin,
                "origin_source": "CONFIG_BOUND",
            }
            if probe:
                try:
                    inventory = probe_ollama_inventory(server.origin)
                    entry["live"] = {
                        "status": "LIVE_ATTESTED",
                        "runtime_version": inventory.runtime_version,
                        "models": [
                            {"name": item.name, "digest": item.digest}
                            for item in inventory.models
                        ],
                    }
                except ValueError as exc:
                    entry["live"] = {
                        "status": "UNREACHABLE",
                        "reason": str(exc) or "runtime_probe_unavailable",
                    }
            servers.append(entry)
        workers = []
        for worker in cfg.workers:
            attestation = None
            if probe:
                attestation = asdict(attest_worker(cfg, worker))
            workers.append({
                "worker_id": worker.worker_id,
                "kind": worker.kind,
                "network_class": worker.network_class,
                "ollama_server_id": worker.ollama_server_id,
                "model_tag": {"value": worker.model_tag, "source": "CONFIG_BOUND"},
                "approved_model_digest": {
                    "value": worker.approved_model_digest,
                    "source": "CONFIG_BOUND" if worker.approved_model_digest else "UNVERIFIED",
                },
                "approved_runtime_version": {
                    "value": worker.approved_runtime_version,
                    "source": "CONFIG_BOUND" if worker.approved_runtime_version else "UNVERIFIED",
                },
                "effective_context_tokens": {
                    "value": worker.effective_context_tokens,
                    "source": "CONFIG_BOUND",
                    "measured_by_ollama": False,
                },
                "temperature": {"value": worker.temperature, "source": "CONFIG_BOUND"},
                "normalizer_id": {"value": worker.normalizer_id, "source": "CONFIG_BOUND"},
                "normalizer_version": {
                    "value": worker.normalizer_version, "source": "CONFIG_BOUND",
                },
                "identity_approved": worker.identity_approved,
                "attestation": attestation,
            })
        return {"ollama_servers": servers, "workers": workers}

    def add_ollama_server(self, server_id: str, origin: str) -> dict:
        try:
            probe_ollama_inventory(origin)
        except ValueError as exc:
            # Still allow saving an origin only after it is a valid origin
            # AND reachable — discovery is not approval of a model digest.
            reason = str(exc) or "runtime_probe_unavailable"
            if reason.startswith("ollama_root"):
                raise APIError(reason, "Ollama origin is not allowed.", 400) from None
            raise APIError(reason, "Ollama server is unreachable.", 409) from None
        cfg = self._app.persistent_config()
        cfg = cfg.with_ollama_server(OllamaServerConfig(server_id=server_id, origin=origin))
        try:
            self._app.save_persistent_config(cfg)
        except ConfigError as exc:
            raise APIError("invalid_config", str(exc), 400) from None
        return self.runtime_overview(probe=False)

    def test_ollama_server(self, server_id: str) -> dict:
        cfg = self._app.persistent_config()
        server = cfg.server_by_id(server_id)
        if server is None:
            raise APIError("not_found", "Ollama server is not configured.", 404)
        try:
            inventory = probe_ollama_inventory(server.origin)
        except ValueError as exc:
            raise APIError(
                str(exc) or "runtime_probe_unavailable",
                "Ollama probe failed.",
                409,
            ) from None
        return {
            "id": server.server_id,
            "origin": server.origin,
            "status": "LIVE_ATTESTED",
            "runtime_version": inventory.runtime_version,
            "models": [
                {"name": item.name, "digest": item.digest} for item in inventory.models
            ],
        }

    def register_worker(self, data: dict) -> dict:
        cfg = self._app.persistent_config()
        if cfg.server_by_id(data["ollama_server_id"]) is None:
            raise APIError("not_found", "Ollama server is not configured.", 404)
        existing = cfg.worker_by_id(data["worker_id"])
        digest = existing.approved_model_digest if existing else None
        version = existing.approved_runtime_version if existing else None
        try:
            worker = WorkerRuntimeConfig(
                worker_id=data["worker_id"],
                kind=data.get("kind", "openai_compatible"),
                network_class=data.get("network_class", "local"),
                ollama_server_id=data["ollama_server_id"],
                model_tag=data["model_tag"],
                approved_model_digest=digest,
                approved_runtime_version=version,
                effective_context_tokens=int(data.get("effective_context_tokens", 16384)),
                temperature=float(data.get("temperature", 0.0)),
                normalizer_id=data.get("normalizer_id") or None,
                normalizer_version=(
                    int(data["normalizer_version"])
                    if data.get("normalizer_version") is not None
                    else None
                ),
            )
        except ConfigError as exc:
            raise APIError("invalid_config", str(exc), 400) from None
        try:
            self._app.save_persistent_config(cfg.with_worker(worker))
        except ConfigError as exc:
            raise APIError("invalid_config", str(exc), 400) from None
        return self.runtime_overview(probe=False)

    def approve_worker_identity(self, worker_id: str, *, allow_replace: bool) -> dict:
        cfg = self._app.persistent_config()
        worker = cfg.worker_by_id(worker_id)
        if worker is None:
            raise APIError("not_found", "Worker is not configured.", 404)
        attestation = attest_worker(cfg, worker)
        if attestation.observed_digest is None or attestation.observed_version is None:
            raise APIError(
                attestation.reason, "Cannot approve without a live-attested digest.", 409,
            )
        if (
            worker.identity_approved
            and not allow_replace
            and attestation.status == "MISMATCH"
        ):
            return {
                "status": "MISMATCH",
                "reason": "runtime_identity_mismatch",
                "configured_digest": worker.approved_model_digest,
                "observed_digest": attestation.observed_digest,
                "configured_version": worker.approved_runtime_version,
                "observed_version": attestation.observed_version,
                "replaced": False,
            }
        updated = WorkerRuntimeConfig(
            worker_id=worker.worker_id,
            kind=worker.kind,
            network_class=worker.network_class,
            ollama_server_id=worker.ollama_server_id,
            model_tag=worker.model_tag,
            approved_model_digest=attestation.observed_digest,
            approved_runtime_version=attestation.observed_version,
            effective_context_tokens=worker.effective_context_tokens,
            temperature=worker.temperature,
            normalizer_id=worker.normalizer_id,
            normalizer_version=worker.normalizer_version,
            output_token_budget=worker.output_token_budget,
            tool_choice_enforcement=worker.tool_choice_enforcement,
            planner_policy_version=worker.planner_policy_version,
        )
        self._app.save_persistent_config(cfg.with_worker(updated))
        return {
            "status": "VERIFIED",
            "reason": "approved_from_live_attestation",
            "configured_digest": updated.approved_model_digest,
            "observed_digest": attestation.observed_digest,
            "replaced": allow_replace and worker.identity_approved,
            "certificates_transferred": False,
        }

    def restart(self) -> dict:
        try:
            restart_service()
        except ProcessError as exc:
            raise APIError(exc.code, "Service restart failed.", 409) from None
        return {"status": "restarted", "unit": "codeslayer.service"}

    def update_check(self) -> dict:
        cfg = self._app.persistent_config()
        checkout = Path(cfg.server.checkout or self._app.repo_path)
        try:
            result = check_for_update(checkout)
        except ProcessError as exc:
            raise APIError(exc.code, "Update check failed.", 409) from None
        return asdict(result)

    def update_apply(self) -> dict:
        cfg = self._app.persistent_config()
        checkout = Path(cfg.server.checkout or self._app.repo_path)
        try:
            result = apply_update(checkout)
        except ProcessError as exc:
            raise APIError(exc.code, "Update refused or failed.", 409) from None
        restart_error = None
        try:
            restart_service()
        except ProcessError as exc:
            restart_error = exc.code
        payload = asdict(result)
        payload["restart_requested"] = restart_error is None
        payload["restart_error"] = restart_error
        payload["deployment_complete"] = False
        payload["deployment_note"] = (
            "git merge is not a completed deployment; poll GET /api/system "
            "for process_commit == checkout_head with process_commit_source "
            "VERIFIED and a clean checkout after restart"
        )
        return payload

    def tailscale_view(self) -> dict:
        cfg = self._app.persistent_config()
        view = tailscale_status(
            backend_host=cfg.server.host, backend_port=cfg.server.port,
        )
        return {
            "node": {"state": view.node_state, "source": view.node_source},
            "serve": {
                "status": view.serve_status,
                "source": view.serve_source,
                "expected_backend": view.expected_backend,
                "observed_backend": view.observed_backend,
                "funnel_detected": view.funnel_detected,
            },
            "remote_access": view.remote_access,
            "url": view.url,
            "backend": view.expected_backend,
            "enabled": cfg.tailscale.enabled,
            "enabled_source": "CONFIG_BOUND",
            "detail": view.detail,
            "source": view.source,
        }

    def tailscale_set(self, enabled: bool) -> dict:
        cfg = self._app.persistent_config()
        try:
            if enabled:
                enable_serve(backend_host=cfg.server.host, backend_port=cfg.server.port)
            else:
                disable_serve()
        except ProcessError as exc:
            raise APIError(exc.code, "Tailscale operation failed.", 409) from None
        self._app.save_persistent_config(cfg.with_tailscale_enabled(enabled))
        return self.tailscale_view()
