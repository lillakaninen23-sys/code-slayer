"""Exact HTTP Host allowlist. Never suffix wildcards. Never browser input."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from werkzeug.sansio.utils import host_is_trusted

LOOPBACK_TRUSTED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HTTP_DEFAULT_PORT = 80
_HTTPS_DEFAULT_PORT = 443


def normalize_tailscale_dns_name(raw: object) -> str | None:
    """Exact MagicDNS hostname, or None. IPs, wildcards, and suffixes fail closed."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().rstrip(".").lower()
    if not text or len(text) > 253:
        return None
    if "*" in text or text.startswith(".") or "/" in text or " " in text or ":" in text:
        return None
    labels = text.split(".")
    if len(labels) < 2:
        return None
    if all(label.isdigit() for label in labels):
        return None
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        return None
    return text


def exact_static_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for host in hosts:
        if not isinstance(host, str):
            raise ValueError("trusted host must be a string")
        text = host.strip().rstrip(".").lower()
        if text == "::1":
            text = "[::1]"
        if text in LOOPBACK_TRUSTED_HOSTS:
            if text not in out:
                out.append(text)
            continue
        dns = normalize_tailscale_dns_name(text)
        if dns is None:
            raise ValueError("trusted host must be an exact hostname")
        if dns not in out:
            out.append(dns)
    if not out:
        raise ValueError("trusted hosts must not be empty")
    return tuple(out)


class LiveTrustedHosts:
    """Werkzeug iterates this at Host-check time, so Tailscale DNS can appear
    after process start without a restart. Static hosts are never empty, so
    an empty extra discovery cannot open the allowlist to all hosts.
    """

    def __init__(
        self,
        static_hosts: Iterable[str],
        *,
        observer: Callable[[], tuple[str | None, str]],
    ) -> None:
        self._static = exact_static_hosts(static_hosts)
        self._observer = observer

    def __iter__(self):
        seen: list[str] = []
        for host in self._static:
            yield host
            seen.append(host)
        extra, _source = self._observer()
        if extra and extra not in seen and not extra.startswith("."):
            normalized = normalize_tailscale_dns_name(extra)
            if normalized and normalized not in seen:
                yield normalized

    def __bool__(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(list(iter(self)))

    def __contains__(self, item: object) -> bool:
        return item in list(self)


def host_header_accepted(hostname: str | None, trusted) -> bool:
    if not hostname:
        return False
    return bool(host_is_trusted(hostname, trusted))


def parse_browser_origin(raw: object) -> tuple[str, str, int] | None:
    """Strict Origin tuple (scheme, host, port), or None. Never a prefix match."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower() == "null":
        return None
    if any(ch in text for ch in (" ", "\t", ",", "\\")):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if "@" in (parts.netloc or ""):
        return None
    if parts.query or parts.fragment or parts.path:
        return None
    host = _canonical_hostname(parts.hostname)
    if host is None:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = _HTTPS_DEFAULT_PORT if scheme == "https" else _HTTP_DEFAULT_PORT
    if not 1 <= port <= 65535:
        return None
    return scheme, host, port


def parse_http_host_header(raw: object) -> tuple[str, int | None] | None:
    """Host header (host, port-or-None). Port None means the client omitted it."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower() == "null":
        return None
    if "://" in text:
        return None
    if any(ch in text for ch in (" ", "\t", "/", "\\", ",", "@", "#", "?")):
        return None
    try:
        parts = urlsplit(f"//{text}")
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    host = _canonical_hostname(parts.hostname)
    if host is None:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return host, port


def origin_allowed(
    origin: object,
    host_header: object,
    *,
    bind_port: int,
    observed_dns_name: str | None,
) -> bool:
    """Allow a browser Origin from server-owned loopback bind and MagicDNS.

    Origin and Host must name the same host. Loopback WebUI is HTTP on the
    configured bind port (or HTTP's default port when Host omitted the port).
    Tailscale WebUI is HTTPS :443 on the exact observed machine MagicDNS.
    Caller supplies the already-validated Host and observed DNS; proxy
    headers are not inputs.
    """
    if isinstance(bind_port, bool) or not isinstance(bind_port, int):
        return False
    if not 1 <= bind_port <= 65535:
        return False
    parsed_origin = parse_browser_origin(origin)
    parsed_host = parse_http_host_header(host_header)
    if parsed_origin is None or parsed_host is None:
        return False
    scheme, origin_host, origin_port = parsed_origin
    host_name, host_port = parsed_host
    if origin_host != host_name:
        return False
    if origin_host in _LOOPBACK_NAMES:
        return _loopback_origin_allowed(scheme, origin_port, host_port, bind_port)
    dns = normalize_tailscale_dns_name(observed_dns_name)
    if dns is None or origin_host != dns:
        return False
    if scheme != "https" or origin_port != _HTTPS_DEFAULT_PORT:
        return False
    return host_port in (None, _HTTPS_DEFAULT_PORT)


def _canonical_hostname(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().rstrip(".").lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text or any(ch in text for ch in ("/", " ", "\t", "*", "@")):
        return None
    return text


def _loopback_origin_allowed(
    scheme: str,
    origin_port: int,
    host_port: int | None,
    bind_port: int,
) -> bool:
    if scheme != "http":
        return False
    if host_port is None:
        return origin_port == _HTTP_DEFAULT_PORT
    return host_port == origin_port == bind_port
