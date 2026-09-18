"""Allowlisted Tailscale Serve operations. Never Funnel. Never shell."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from code_slayer.admin.hosts import (
    LOOPBACK_TRUSTED_HOSTS,
    LiveTrustedHosts,
    host_header_accepted,
    normalize_tailscale_dns_name,
)
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
    dns_name: str | None = None
    dns_name_source: str = "UNVERIFIED"
    host_accepted: bool = False
    host_accepted_source: str = "UNVERIFIED"
    serve_hosts: tuple[str, ...] = ()
    detail: str = ""
    source: str = "tailscale"

    @property
    def backend(self) -> str:
        return self.expected_backend

    @property
    def state(self) -> str:
        """Summary of CSLR remote access, never node connectivity alone."""
        return self.remote_access


def observe_self_dns_name(*, runner=None) -> tuple[str | None, str]:
    """Machine Tailscale DNS from local `status --json` Self.DNSName only."""
    run = runner or run_fixed
    try:
        result = run((TAILSCALE, "status", "--json"), timeout=5.0)
    except ProcessError:
        return None, "UNVERIFIED"
    if result.returncode != 0:
        return None, "UNVERIFIED"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "UNVERIFIED"
    if not isinstance(payload, dict):
        return None, "UNVERIFIED"
    return _dns_from_status_payload(payload)


def status(
    *,
    runner=None,
    backend_host: str = "127.0.0.1",
    backend_port: int = 8765,
    static_trusted_hosts: tuple[str, ...] = LOOPBACK_TRUSTED_HOSTS,
) -> TailscaleView:
    run = runner or run_fixed
    expected = f"http://{backend_host}:{backend_port}"
    node_state, node_source, url, node_detail, dns_name, dns_source = _node_status(run)
    serve_status, serve_source, observed, funnel, serve_detail, serve_hosts = _serve_status(
        run, expected_backend=expected,
    )
    trusted = LiveTrustedHosts(
        static_trusted_hosts,
        observer=lambda: (dns_name, dns_source),
    )
    candidate = _remote_host_candidate(dns_name=dns_name, serve_hosts=serve_hosts)
    accepted = host_header_accepted(candidate, trusted)
    remote = _remote_access(
        node_state=node_state,
        serve_status=serve_status,
        funnel=funnel,
        host_accepted=accepted,
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
        dns_name=dns_name,
        dns_name_source=dns_source,
        host_accepted=accepted,
        host_accepted_source="VERIFIED" if accepted else "UNVERIFIED",
        serve_hosts=serve_hosts,
        detail=detail,
    )


def intent_alignment(enabled: bool, serve_status: str) -> str:
    if serve_status in {"ERROR", "UNVERIFIED"}:
        return "UNVERIFIED"
    live = serve_status != "not_configured"
    if enabled is live:
        return "VERIFIED"
    return "MISMATCH"


def expected_serve_mapping(view: TailscaleView) -> bool:
    """Live Serve proxies the configured loopback backend and is not Funnel.

    ``serve_status == VERIFIED`` is this mapping only. It is not by itself
    a usable remote path and must not be used as an adoption predicate.
    """
    return (
        view.serve_status == "VERIFIED"
        and view.serve_source == "LIVE_ATTESTED"
        and not view.funnel_detected
    )


def exact_desired_live_serve(view: TailscaleView) -> bool:
    """Full usable CSLR remote path. The only live state that may be adopted."""
    return (
        expected_serve_mapping(view)
        and view.node_state == "Connected"
        and view.host_accepted is True
        and view.remote_access == "VERIFIED"
    )


def serve_absent_attested(view: TailscaleView) -> bool:
    """Empty Serve mapping from a well-formed live ``serve status --json``.

    Distinguishes ``not_configured`` from ERROR/UNVERIFIED/empty/malformed.
    """
    return (
        view.serve_status == "not_configured"
        and view.serve_source == "LIVE_ATTESTED"
        and not view.funnel_detected
    )


def serve_ready_to_configure(view: TailscaleView) -> bool:
    """Attested-absent Serve that can be verified after the allowlisted set.

    Node must already be Connected and the MagicDNS Host already accepted.
    Otherwise a successful ``serve --bg`` cannot satisfy
    ``exact_desired_live_serve`` and would mutate without a persistable end
    state.
    """
    return (
        serve_absent_attested(view)
        and view.node_state == "Connected"
        and view.host_accepted is True
    )


def plan_enable(view: TailscaleView) -> str:
    """Fail-closed enable plan: ``adopt``, ``configure``, or a reject code."""
    if view.funnel_detected:
        return "tailscale_funnel_detected"
    if view.serve_status == "ERROR":
        return "tailscale_serve_error"
    if view.serve_status == "UNVERIFIED":
        return "tailscale_serve_unverified"
    if exact_desired_live_serve(view):
        return "adopt"
    if serve_ready_to_configure(view):
        return "configure"
    if view.serve_status == "MISMATCH":
        return "tailscale_serve_mismatch"
    if view.serve_status == "VERIFIED":
        if view.node_state != "Connected":
            return "tailscale_node_not_connected"
        if not view.host_accepted:
            return "tailscale_host_not_accepted"
        return "tailscale_remote_unverified"
    if view.serve_status == "not_configured":
        if view.node_state != "Connected":
            return "tailscale_node_not_connected"
        if not view.host_accepted:
            return "tailscale_host_not_accepted"
        return "tailscale_serve_unverified"
    return "tailscale_serve_unverified"


def plan_disable(view: TailscaleView) -> str:
    """Fail-closed disable plan: ``clear_intent``, ``reset``, or a reject code."""
    if view.funnel_detected:
        return "tailscale_funnel_detected"
    if view.serve_status == "ERROR":
        return "tailscale_serve_error"
    if view.serve_status == "UNVERIFIED":
        return "tailscale_serve_unverified"
    if serve_absent_attested(view):
        return "clear_intent"
    if expected_serve_mapping(view):
        return "reset"
    if view.serve_status == "MISMATCH":
        return "tailscale_serve_mismatch"
    return "tailscale_serve_unverified"


def _dns_from_status_payload(payload: dict) -> tuple[str | None, str]:
    self_doc = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    raw = self_doc.get("DNSName") if isinstance(self_doc, dict) else None
    name = normalize_tailscale_dns_name(raw)
    if name is None:
        return None, "UNVERIFIED"
    return name, "VERIFIED"


def _node_status(run) -> tuple[str, str, str | None, str, str | None, str]:
    try:
        result = run((TAILSCALE, "status", "--json"), timeout=10.0)
    except ProcessError as exc:
        if exc.code == "executable_missing":
            return "Disabled", "UNVERIFIED", None, "tailscale_not_installed", None, "UNVERIFIED"
        return "Error", "UNVERIFIED", None, exc.code, None, "UNVERIFIED"
    if result.returncode != 0:
        return "Error", "UNVERIFIED", None, "tailscale_status_failed", None, "UNVERIFIED"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "Error", "UNVERIFIED", None, "tailscale_status_malformed", None, "UNVERIFIED"
    if not isinstance(payload, dict):
        return "Error", "UNVERIFIED", None, "tailscale_status_malformed", None, "UNVERIFIED"
    dns_name, dns_source = _dns_from_status_payload(payload)
    url = f"https://{dns_name}" if dns_name else None
    backend_state = payload.get("BackendState")
    if backend_state == "Running":
        return "Connected", "OBSERVED", url, str(backend_state), dns_name, dns_source
    if backend_state in {None, "Stopped", "NeedsLogin"}:
        return "Disabled", "OBSERVED", url, str(backend_state or "Stopped"), dns_name, dns_source
    return "Error", "OBSERVED", url, str(backend_state or ""), dns_name, dns_source


def _serve_status(
    run, *, expected_backend: str,
) -> tuple[str, str, str | None, bool, str, tuple[str, ...]]:
    try:
        result = run((TAILSCALE, "serve", "status", "--json"), timeout=10.0)
    except ProcessError as exc:
        if exc.code == "executable_missing":
            return "UNVERIFIED", "UNVERIFIED", None, False, "tailscale_not_installed", ()
        return "ERROR", "UNVERIFIED", None, False, exc.code, ()
    if result.returncode != 0:
        return "ERROR", "UNVERIFIED", None, False, "tailscale_serve_status_failed", ()
    raw = (result.stdout or "").strip()
    if not raw:
        return "UNVERIFIED", "UNVERIFIED", None, False, "tailscale_serve_status_empty", ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "ERROR", "UNVERIFIED", None, False, "tailscale_serve_status_malformed", ()
    if not isinstance(payload, dict):
        return "ERROR", "UNVERIFIED", None, False, "tailscale_serve_status_malformed", ()
    funnel = _allow_funnel_enabled(payload)
    proxies = _collect_proxies(payload)
    hosts = tuple(_serve_web_hosts(payload))
    if funnel:
        observed = proxies[0] if proxies else None
        return "MISMATCH", "LIVE_ATTESTED", observed, True, "funnel_detected", hosts
    if not proxies:
        return "not_configured", "LIVE_ATTESTED", None, False, "serve_not_configured", hosts
    expected_norm = _normalize_proxy(expected_backend)
    matching = [item for item in proxies if _normalize_proxy(item) == expected_norm]
    if matching:
        return "VERIFIED", "LIVE_ATTESTED", matching[0], False, "serve_backend_matches", hosts
    return "MISMATCH", "LIVE_ATTESTED", proxies[0], False, "serve_backend_mismatch", hosts


def _remote_host_candidate(*, dns_name: str | None, serve_hosts: tuple[str, ...]) -> str | None:
    if dns_name is None:
        return None
    if serve_hosts and dns_name not in serve_hosts:
        return None
    return dns_name


def _remote_access(
    *, node_state: str, serve_status: str, funnel: bool, host_accepted: bool,
) -> str:
    if funnel or serve_status == "MISMATCH":
        return "MISMATCH"
    if node_state == "Connected" and serve_status == "VERIFIED" and host_accepted:
        return "VERIFIED"
    if serve_status == "ERROR":
        return "ERROR"
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


def _serve_web_hosts(doc: dict) -> list[str]:
    web = doc.get("Web")
    if not isinstance(web, dict):
        return []
    hosts: list[str] = []
    for key in web:
        if not isinstance(key, str):
            continue
        host = normalize_tailscale_dns_name(key.split("/", 1)[0].split(":")[0])
        if host and host not in hosts:
            hosts.append(host)
    return hosts


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
    # Non-interactive (no TTY, systemd user service):
    # `tailscale serve --bg <backend>` is serve-set. That FlagSet registers
    # --yes, but e.yes is only read in removeWebServe when deleting more than
    # one mount (prompt.YesNo unless --yes). A set does not prompt. Do not
    # pass --yes: it is unused here and would imply a prompt this path does
    # not have.
    result = run((TAILSCALE, "serve", "--bg", backend), timeout=15.0)
    if result.returncode != 0:
        raise ProcessError("tailscale_serve_failed", result.stderr.strip())
    return result


def disable_serve(*, runner=None):
    run = runner or run_fixed
    # `tailscale serve reset` FlagSet is serve-reset with nil flags.
    # --yes is not registered (`flag provided but not defined` if passed).
    # runServeReset writes an empty ServeConfig; it does not prompt.
    result = run((TAILSCALE, "serve", "reset"), timeout=15.0)
    if result.returncode != 0:
        raise ProcessError("tailscale_serve_reset_failed", result.stderr.strip())
    return result
