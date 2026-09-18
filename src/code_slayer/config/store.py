"""Load and atomically save persistent CSLR configuration as TOML."""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

from code_slayer.config.paths import config_path as resolve_config_path
from code_slayer.config.schema import (
    ConfigError,
    CSLRConfig,
    OllamaServerConfig,
    WorkerRuntimeConfig,
)


def load_config(*, path: str | Path | None = None) -> CSLRConfig:
    resolved = resolve_config_path(override=path)
    if not resolved.is_file():
        return CSLRConfig()
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ConfigError("config_unreadable") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("config_malformed") from exc
    return CSLRConfig.from_mapping(data)


def save_config(config: CSLRConfig, *, path: str | Path | None = None) -> Path:
    if not isinstance(config, CSLRConfig):
        raise ConfigError("config must be a CSLRConfig")
    resolved = resolve_config_path(override=path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = render_toml(config).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix="codeslayer-config-", dir=str(resolved.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, resolved)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return resolved


def render_toml(config: CSLRConfig) -> str:
    lines = [
        "# Code Slayer machine-local configuration. Do not commit this file.",
        "# Fingerprints are derived from approved fields at runtime.",
        "",
        "[server]",
        f"host = {_toml_str(config.server.host)}",
        f"port = {config.server.port}",
    ]
    if config.server.checkout:
        lines.append(f"checkout = {_toml_str(config.server.checkout)}")
    if config.server.webui_dir:
        lines.append(f"webui_dir = {_toml_str(config.server.webui_dir)}")
    lines.extend([
        "",
        "[tailscale]",
        f"enabled = {'true' if config.tailscale.enabled else 'false'}",
        "",
    ])
    for server in config.ollama_servers:
        lines.extend(_render_ollama(server))
    for worker in config.workers:
        lines.extend(_render_worker(worker))
    return "\n".join(lines) + "\n"


def _render_ollama(server: OllamaServerConfig) -> list[str]:
    return [
        "[[ollama_servers]]",
        f"id = {_toml_str(server.server_id)}",
        f"origin = {_toml_str(server.origin)}",
        "",
    ]


def _render_worker(worker: WorkerRuntimeConfig) -> list[str]:
    lines = [
        "[[workers]]",
        f"worker_id = {_toml_str(worker.worker_id)}",
        f"kind = {_toml_str(worker.kind)}",
        f"network_class = {_toml_str(worker.network_class)}",
        f"ollama_server_id = {_toml_str(worker.ollama_server_id)}",
        f"model_tag = {_toml_str(worker.model_tag)}",
        f"effective_context_tokens = {worker.effective_context_tokens}",
        f"temperature = {worker.temperature}",
        f"output_token_budget = {worker.output_token_budget}",
        f"tool_choice_enforcement = {_toml_str(worker.tool_choice_enforcement)}",
        f"planner_policy_version = {_toml_str(worker.planner_policy_version)}",
    ]
    if worker.approved_model_digest:
        lines.append(f"approved_model_digest = {_toml_str(worker.approved_model_digest)}")
    if worker.approved_runtime_version:
        lines.append(
            f"approved_runtime_version = {_toml_str(worker.approved_runtime_version)}"
        )
    if worker.normalizer_id is not None:
        lines.append(f"normalizer_id = {_toml_str(worker.normalizer_id)}")
    if worker.normalizer_version is not None:
        lines.append(f"normalizer_version = {worker.normalizer_version}")
    lines.append("")
    return lines


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
