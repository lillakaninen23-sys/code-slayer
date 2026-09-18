"""Allowlisted Tailscale Serve operations. Never Funnel. Never shell."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from code_slayer.admin.process import ProcessError, run_fixed
from code_slayer.config.schema import LOOPBACK_HOSTS

TAILSCALE = "tailscale"


@dataclass(frozen=True)
class TailscaleView:
    node_state: str
    node_source: str
    serve_status: str
    serve_source: str
    remote_access: str
    url: str | None
    expected_backend: str
    observed_backend: str | None
    funnel_detected: bool
    detail: str = ""
    source: str = "tailscale"

    @property
    def backend(self) -> str:
        return self.expected_backend

    @property
    def state(self) -> str:
        """Summary of CSLR remote access, never node connectivity alone."""
        return self.remote_access


def status(
    *,
    runner=None,
    backend_host: str = "127.0.0.1",
    backend_port: int = 8765,
) -> TailscaleView:
    run = runner or run_fixed
    expected = f"http://{backend_host}:{backend_port}"
    node_state, node_source, url, node_detail = _node_status(run)
    serve_status, serve_source, observed, funnel, serve_detail = _serve_status(
        run, expected_backend=expected,
    )
    remote = _remote_access(
        node_state=node_state, serve_status=serve_status, funnel=funnel,
    )
    detail = "; ".join(part for part in (node_detail, serve_detail) if part)
    return TailscaleView(
        node_state=node_state,
        node_source=node_source,
        serve_status=serve_status,
        serve_source=serve_source,
        remote_access=remote,
        url=url,
        expected_backend=expected,
        observed_backend=observed,
        funnel_detected=funnel,
        detail=detail,
    )


def _node_status(run) -> tuple[str, str, str | None, str]:
    try:
        result = run((TAILSCALE, "status", "--json"), timeout=10.0)
    except ProcessError as exc:
        if exc.code == "executable_missing":
            return "Disabled", "UNVERIFIED", None, "tailscale_not_installed"
        return "Error", "UNVERIFIED", None, exc.code
    if result.returncode != 0:
        return "Error", "UNVERIFIED", None, "tailscale_status_failed"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "Error", "UNVERIFIED", None, "tailscale_status_malformed"
    if not isinstance(payload, dict):
        return "Error", "UNVERIFIED", None, "tailscale_status_malformed"
    backend_state = payload.get("BackendState")
    self_doc = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    dns = self_doc.get("DNSName") if isinstance(self_doc, dict) else None
    url = None
    if isinstance(dns, str) and dns.strip():
        url = f"https://{dns.strip().rstrip('.')}"
    if backend_state == "Running":
        return "Connected", "OBSERVED", url, str(backend_state)
    if backend_state in {None, "Stopped", "NeedsLogin"}:
        return "Disabled", "OBSERVED", url, str(backend_state or "Stopped")
    return "Error", "OBSERVED", url, str(backend_state or "")


def _serve_status(
    run, *, expected_backend: str,
) -> tuple[str, str, str | None, bool, str]:
    try:
        result = run((TAILSCALE, "serve", "status", "--json"), timeout=10.0)
    except ProcessError as exc:
        if exc.code == "executable_missing":
            return "UNVERIFIED", "UNVERIFIED", None, False, "tailscale_not_installed"
        return "ERROR", "UNVERIFIED", None, False, exc.code
    if result.returncode != 0:
        return "ERROR", "UNVERIFIED", None, False, "tailscale_serve_status_failed"
    raw = (result.stdout or "").strip()
    if not raw:
        return "UNVERIFIED", "UNVERIFIED", None, False, "tailscale_serve_status_empty"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "ERROR", "UNVERIFIED", None, False, "tailscale_serve_status_malformed"
    if not isinstance(payload, dict):
        return "ERROR", "UNVERIFIED", None, False, "tailscale_serve_status_malformed"
    funnel = _allow_funnel_enabled(payload)
    proxies = _collect_proxies(payload)
    if funnel:
        observed = proxies[0] if proxies else None
        return "MISMATCH", "LIVE_ATTESTED", observed, True, "funnel_detected"
    if not proxies:
        return "not_configured", "LIVE_ATTESTED", None, False, "serve_not_configured"
    expected_norm = _normalize_proxy(expected_backend)
    matching = [item for item in proxies if _normalize_proxy(item) == expected_norm]
    if matching:
        return "VERIFIED", "LIVE_ATTESTED", matching[0], False, "serve_backend_matches"
    return "MISMATCH", "LIVE_ATTESTED", proxies[0], False, "serve_backend_mismatch"


def _remote_access(*, node_state: str, serve_status: str, funnel: bool) -> str:
    if funnel or serve_status == "MISMATCH":
        return "MISMATCH"
    if node_state == "Connected" and serve_status == "VERIFIED":
        return "VERIFIED"
    if serve_status in {"ERROR", "UNVERIFIED"}:
        return serve_status if serve_status == "ERROR" else "UNVERIFIED"
    return "UNVERIFIED"


def _allow_funnel_enabled(doc: object) -> bool:
    if isinstance(doc, dict):
        allow = doc.get("AllowFunnel")
        if isinstance(allow, dict) and any(bool(value) for value in allow.values()):
            return True
        return any(_allow_funnel_enabled(value) for value in doc.values())
    if isinstance(doc, list):
        return any(_allow_funnel_enabled(item) for item in doc)
    return False


def _collect_proxies(doc: object) -> list[str]:
    found: list[str] = []
    if isinstance(doc, dict):
        proxy = doc.get("Proxy")
        if isinstance(proxy, str) and proxy.strip():
            found.append(proxy.strip())
        for value in doc.values():
            found.extend(_collect_proxies(value))
    elif isinstance(doc, list):
        for item in doc:
            found.extend(_collect_proxies(item))
    return found


def _normalize_proxy(value: str) -> str:
    text = value.strip().rstrip("/")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "::1"}:
        host = "127.0.0.1"
    scheme = (parsed.scheme or "http").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    if host == "127.0.0.1":
        return f"{scheme}://127.0.0.1:{port}"
    return f"{scheme}://{host}:{port}"


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
