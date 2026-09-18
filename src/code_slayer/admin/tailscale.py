"""Allowlisted Tailscale Serve operations. Never Funnel. Never shell."""

from __future__ import annotations

import json
from dataclasses import dataclass

from code_slayer.admin.process import ProcessError, run_fixed
from code_slayer.config.schema import LOOPBACK_HOSTS

TAILSCALE = "tailscale"


@dataclass(frozen=True)
class TailscaleView:
    state: str
    url: str | None
    backend: str
    source: str
    detail: str = ""


def status(
    *,
    runner=None,
    backend_host: str = "127.0.0.1",
    backend_port: int = 8765,
) -> TailscaleView:
    run = runner or run_fixed
    backend = f"http://{backend_host}:{backend_port}"
    try:
        result = run((TAILSCALE, "status", "--json"), timeout=10.0)
    except ProcessError as exc:
        if exc.code == "executable_missing":
            return TailscaleView(
                state="Disabled", url=None, backend=backend,
                source="tailscale", detail="tailscale_not_installed",
            )
        return TailscaleView(
            state="Error", url=None, backend=backend,
            source="tailscale", detail=exc.code,
        )
    if result.returncode != 0:
        return TailscaleView(
            state="Error", url=None, backend=backend,
            source="tailscale", detail="tailscale_status_failed",
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return TailscaleView(
            state="Error", url=None, backend=backend,
            source="tailscale", detail="tailscale_status_malformed",
        )
    if not isinstance(payload, dict):
        return TailscaleView(
            state="Error", url=None, backend=backend,
            source="tailscale", detail="tailscale_status_malformed",
        )
    backend_state = payload.get("BackendState")
    self_doc = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    dns = self_doc.get("DNSName") if isinstance(self_doc, dict) else None
    url = None
    if isinstance(dns, str) and dns.strip():
        host = dns.strip().rstrip(".")
        url = f"https://{host}"
    if backend_state == "Running":
        state = "Connected"
    elif backend_state in {None, "Stopped", "NeedsLogin"}:
        state = "Disabled"
    else:
        state = "Error"
    return TailscaleView(
        state=state, url=url, backend=backend, source="tailscale",
        detail=str(backend_state or ""),
    )


def enable_serve(
    *,
    runner=None,
    backend_host: str = "127.0.0.1",
    backend_port: int = 8765,
):
    run = runner or run_fixed
    if backend_host not in LOOPBACK_HOSTS:
        raise ProcessError("tailscale_backend_not_loopback")
    if not 1 <= backend_port <= 65535:
        raise ProcessError("tailscale_backend_port_invalid")
    backend = f"http://{backend_host}:{backend_port}"
    # Serve is tailnet-only. Funnel is never used.
    result = run((TAILSCALE, "serve", "--bg", backend), timeout=15.0)
    if result.returncode != 0:
        raise ProcessError("tailscale_serve_failed", result.stderr.strip())
    return result


def disable_serve(*, runner=None):
    run = runner or run_fixed
    result = run((TAILSCALE, "serve", "reset"), timeout=15.0)
    if result.returncode != 0:
        raise ProcessError("tailscale_serve_reset_failed", result.stderr.strip())
    return result
