"""Persistent, machine-local CSLR configuration.

Operator-facing runtime identity lives here, never in the Git checkout
and never as shell-exported `CODESLAYER_CERT_*` variables. Fingerprints
are derived from approved fields, never stored as independent authority.
"""

from code_slayer.config.bindings import runtime_bindings_from_config
from code_slayer.config.paths import config_path, default_config_dir
from code_slayer.config.schema import (
    CSLRConfig,
    OllamaServerConfig,
    ServerConfig,
    TailscaleConfig,
    WorkerRuntimeConfig,
)
from code_slayer.config.store import ConfigError, load_config, save_config

__all__ = [
    "CSLRConfig",
    "ConfigError",
    "OllamaServerConfig",
    "ServerConfig",
    "TailscaleConfig",
    "WorkerRuntimeConfig",
    "config_path",
    "default_config_dir",
    "load_config",
    "runtime_bindings_from_config",
    "save_config",
]
