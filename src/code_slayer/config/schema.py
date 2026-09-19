"""Typed persistent configuration. Unknown keys fail closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
ALLOWED_WORKER_KINDS = frozenset({"openai_compatible"})
ALLOWED_NETWORK_CLASSES = frozenset({"local"})
ALLOWED_NORMALIZER_IDS = frozenset({"qwen_textual_tool_v1"})


class ConfigError(ValueError):
    """Invalid persistent configuration. Fail closed."""


def _require_str(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise ConfigError(f"{name} must be a non-empty string")
    return text


def _require_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} is out of range")
    return value


def _require_float(value: object, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} is out of range")
    return number


def _reject_unknown(data: dict, allowed: set[str], name: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise ConfigError(f"{name} has unsupported fields: {sorted(extra)}")


def _validate_http_origin(url: str, name: str) -> None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(f"{name} must be an http(s) origin")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"{name} must not include userinfo")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must not include query or fragment")
    path = parsed.path.rstrip("/")
    if path:
        raise ConfigError(f"{name} must be origin-only")
    if not parsed.hostname:
        raise ConfigError(f"{name} must include a host")


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    checkout: str | None = None
    webui_dir: str | None = None

    def __post_init__(self) -> None:
        if self.host not in LOOPBACK_HOSTS:
            raise ConfigError("server.host must be a loopback address")
        if not 1 <= self.port <= 65535:
            raise ConfigError("server.port is out of range")

    @classmethod
    def from_mapping(cls, raw: object) -> ServerConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError("server must be a table")
        _reject_unknown(raw, {"host", "port", "checkout", "webui_dir"}, "server")
        host = _require_str(raw["host"], "server.host") if "host" in raw else "127.0.0.1"
        port = _require_int(raw["port"], "server.port", minimum=1, maximum=65535) if (
            "port" in raw
        ) else 8765
        checkout = None
        if "checkout" in raw and raw["checkout"] is not None:
            checkout = _require_str(raw["checkout"], "server.checkout")
        webui_dir = None
        if "webui_dir" in raw and raw["webui_dir"] is not None:
            webui_dir = _require_str(raw["webui_dir"], "server.webui_dir")
        return cls(host=host, port=port, checkout=checkout, webui_dir=webui_dir)


@dataclass(frozen=True)
class TailscaleConfig:
    enabled: bool = False

    @classmethod
    def from_mapping(cls, raw: object) -> TailscaleConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError("tailscale must be a table")
        _reject_unknown(raw, {"enabled"}, "tailscale")
        enabled = False
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                raise ConfigError("tailscale.enabled must be a boolean")
            enabled = raw["enabled"]
        return cls(enabled=enabled)


@dataclass(frozen=True)
class OllamaServerConfig:
    server_id: str
    origin: str

    def __post_init__(self) -> None:
        if not self.server_id or "/" in self.server_id or " " in self.server_id:
            raise ConfigError("ollama server_id is invalid")
        _validate_http_origin(self.origin, "ollama server origin")

    @classmethod
    def from_mapping(cls, raw: object) -> OllamaServerConfig:
        if not isinstance(raw, dict):
            raise ConfigError("ollama server must be a table")
        _reject_unknown(raw, {"id", "origin"}, "ollama server")
        return cls(
            server_id=_require_str(raw.get("id"), "ollama server id"),
            origin=_require_str(raw.get("origin"), "ollama server origin"),
        )


@dataclass(frozen=True)
class WorkerRuntimeConfig:
    worker_id: str
    kind: str
    network_class: str
    ollama_server_id: str
    model_tag: str
    approved_model_digest: str | None
    approved_runtime_version: str | None
    effective_context_tokens: int
    temperature: float
    normalizer_id: str | None
    normalizer_version: int | None
    output_token_budget: int = 4096
    tool_choice_enforcement: str = "ADVISORY_ONLY_UNVERIFIED"
    planner_policy_version: str = "planner-certification-v2"
    # H.4.1: the actual Planner INFERENCE request timeout (`OpenAICompatibleConfig.
    # timeout` for `/v1/chat/completions` calls) -- distinct from `security.
    # live_certification.LiveOllamaRuntimeExpectation.timeout`, which bounds
    # `/api/version`/`/api/tags` runtime-attestation probe traffic. Default
    # preserves the adapter's prior hardcoded 30.0s behavior for existing config,
    # never silently rewritten during migration.
    planner_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.worker_id or "/" in self.worker_id or " " in self.worker_id:
            raise ConfigError("worker_id is invalid")
        if self.kind not in ALLOWED_WORKER_KINDS:
            raise ConfigError("worker.kind is not supported")
        if self.network_class not in ALLOWED_NETWORK_CLASSES:
            raise ConfigError("worker.network_class is not supported")
        if (self.normalizer_id is None) != (self.normalizer_version is None):
            raise ConfigError("normalizer_id and normalizer_version must both be set or both empty")
        if self.normalizer_id is not None and self.normalizer_id not in ALLOWED_NORMALIZER_IDS:
            raise ConfigError("worker.normalizer_id is not supported")
        if bool(self.approved_model_digest) != bool(self.approved_runtime_version):
            raise ConfigError(
                "approved_model_digest and approved_runtime_version must both be set or both empty"
            )

    @property
    def identity_approved(self) -> bool:
        return bool(self.approved_model_digest and self.approved_runtime_version)

    @classmethod
    def from_mapping(cls, raw: object) -> WorkerRuntimeConfig:
        if not isinstance(raw, dict):
            raise ConfigError("worker must be a table")
        allowed = {
            "worker_id", "kind", "network_class", "ollama_server_id", "model_tag",
            "approved_model_digest", "approved_runtime_version",
            "effective_context_tokens", "temperature", "normalizer_id",
            "normalizer_version", "output_token_budget", "tool_choice_enforcement",
            "planner_policy_version", "planner_timeout_seconds",
        }
        _reject_unknown(raw, allowed, "worker")
        normalizer_id = None
        if raw.get("normalizer_id"):
            normalizer_id = _require_str(raw.get("normalizer_id"), "worker.normalizer_id")
        normalizer_version = None
        if "normalizer_version" in raw and raw["normalizer_version"] is not None:
            normalizer_version = _require_int(
                raw["normalizer_version"], "worker.normalizer_version",
                minimum=1, maximum=1000,
            )
        digest = None
        if raw.get("approved_model_digest"):
            digest = _require_str(raw.get("approved_model_digest"), "worker.approved_model_digest")
        version = None
        if raw.get("approved_runtime_version"):
            version = _require_str(
                raw.get("approved_runtime_version"), "worker.approved_runtime_version",
            )
        return cls(
            worker_id=_require_str(raw.get("worker_id"), "worker.worker_id"),
            kind=(
                _require_str(raw.get("kind"), "worker.kind")
                if raw.get("kind") else "openai_compatible"
            ),
            network_class=(
                _require_str(raw.get("network_class"), "worker.network_class")
                if raw.get("network_class") else "local"
            ),
            ollama_server_id=_require_str(raw.get("ollama_server_id"), "worker.ollama_server_id"),
            model_tag=_require_str(raw.get("model_tag"), "worker.model_tag"),
            approved_model_digest=digest,
            approved_runtime_version=version,
            effective_context_tokens=_require_int(
                raw.get("effective_context_tokens", 16384),
                "worker.effective_context_tokens", minimum=1, maximum=10_000_000,
            ),
            temperature=_require_float(
                raw.get("temperature", 0.0), "worker.temperature",
                minimum=0.0, maximum=2.0,
            ),
            normalizer_id=normalizer_id,
            normalizer_version=normalizer_version,
            output_token_budget=_require_int(
                raw.get("output_token_budget", 4096), "worker.output_token_budget",
                minimum=1, maximum=1_000_000,
            ),
            tool_choice_enforcement=_require_str(
                raw.get("tool_choice_enforcement", "ADVISORY_ONLY_UNVERIFIED"),
                "worker.tool_choice_enforcement",
            ),
            planner_policy_version=_require_str(
                raw.get("planner_policy_version", "planner-certification-v2"),
                "worker.planner_policy_version",
            ),
            planner_timeout_seconds=_require_float(
                raw.get("planner_timeout_seconds", 30.0), "worker.planner_timeout_seconds",
                minimum=1.0, maximum=1800.0,
            ),
        )


@dataclass(frozen=True)
class CSLRConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    tailscale: TailscaleConfig = field(default_factory=TailscaleConfig)
    ollama_servers: tuple[OllamaServerConfig, ...] = ()
    workers: tuple[WorkerRuntimeConfig, ...] = ()

    def server_by_id(self, server_id: str) -> OllamaServerConfig | None:
        for item in self.ollama_servers:
            if item.server_id == server_id:
                return item
        return None

    def worker_by_id(self, worker_id: str) -> WorkerRuntimeConfig | None:
        for item in self.workers:
            if item.worker_id == worker_id:
                return item
        return None

    def with_ollama_server(self, server: OllamaServerConfig) -> CSLRConfig:
        others = tuple(
            item for item in self.ollama_servers if item.server_id != server.server_id
        )
        return CSLRConfig(
            server=self.server, tailscale=self.tailscale,
            ollama_servers=(*others, server), workers=self.workers,
        )

    def with_worker(self, worker: WorkerRuntimeConfig) -> CSLRConfig:
        others = tuple(item for item in self.workers if item.worker_id != worker.worker_id)
        return CSLRConfig(
            server=self.server, tailscale=self.tailscale,
            ollama_servers=self.ollama_servers, workers=(*others, worker),
        )

    def with_tailscale_enabled(self, enabled: bool) -> CSLRConfig:
        return CSLRConfig(
            server=self.server, tailscale=TailscaleConfig(enabled=enabled),
            ollama_servers=self.ollama_servers, workers=self.workers,
        )

    def with_checkout(self, checkout: str, webui_dir: str | None) -> CSLRConfig:
        server = ServerConfig(
            host=self.server.host, port=self.server.port,
            checkout=checkout, webui_dir=webui_dir,
        )
        return CSLRConfig(
            server=server, tailscale=self.tailscale,
            ollama_servers=self.ollama_servers, workers=self.workers,
        )

    @classmethod
    def from_mapping(cls, raw: object) -> CSLRConfig:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a table")
        _reject_unknown(
            raw, {"server", "tailscale", "ollama_servers", "workers"}, "config",
        )
        servers_raw = raw.get("ollama_servers", [])
        if servers_raw is None:
            servers_raw = []
        if not isinstance(servers_raw, list):
            raise ConfigError("ollama_servers must be an array")
        workers_raw = raw.get("workers", [])
        if workers_raw is None:
            workers_raw = []
        if not isinstance(workers_raw, list):
            raise ConfigError("workers must be an array")
        servers = tuple(OllamaServerConfig.from_mapping(item) for item in servers_raw)
        ids = [item.server_id for item in servers]
        if len(ids) != len(set(ids)):
            raise ConfigError("ollama server ids must be unique")
        workers = tuple(WorkerRuntimeConfig.from_mapping(item) for item in workers_raw)
        worker_ids = [item.worker_id for item in workers]
        if len(worker_ids) != len(set(worker_ids)):
            raise ConfigError("worker ids must be unique")
        known = {item.server_id for item in servers}
        for worker in workers:
            if worker.ollama_server_id not in known:
                raise ConfigError(
                    f"worker {worker.worker_id} references unknown ollama server",
                )
        return cls(
            server=ServerConfig.from_mapping(raw.get("server")),
            tailscale=TailscaleConfig.from_mapping(raw.get("tailscale")),
            ollama_servers=servers,
            workers=workers,
        )
