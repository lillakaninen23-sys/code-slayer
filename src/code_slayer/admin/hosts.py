"""Exact HTTP Host allowlist. Never suffix wildcards. Never browser input."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from werkzeug.sansio.utils import host_is_trusted

LOOPBACK_TRUSTED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


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
