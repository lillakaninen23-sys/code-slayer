"""Typed capability requests. File bytes never form audit payloads."""

from dataclasses import dataclass, field
from enum import StrEnum


class RiskClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    WRITE_OWNED = "WRITE_OWNED"
    WRITE_EXISTING = "WRITE_EXISTING"
    EXECUTE_SAFE = "EXECUTE_SAFE"
    GIT_READ = "GIT_READ"
    GIT_MUTATION = "GIT_MUTATION"
    NETWORK = "NETWORK"
    DESTRUCTIVE = "DESTRUCTIVE"


class ToolError(RuntimeError):
    """Stable reason code; never include file, command output, or environment bytes."""


@dataclass(frozen=True)
class PatchHunk:
    offset: int
    before: bytes = field(repr=False)
    after: bytes = field(repr=False)


@dataclass(frozen=True)
class CommandRequest:
    profile: str
    executable: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout: float = 3.0
    output_limit: int = 65536


@dataclass(frozen=True)
class ToolRequest:
    tool: str
    path: str | None = None
    content: bytes | None = field(default=None, repr=False)
    expected_hash: str | None = None
    hunks: tuple[PatchHunk, ...] = ()
    command: CommandRequest | None = None
    network: bool = False


@dataclass(frozen=True)
class ToolResult:
    operation_id: str
    decision: str
    reason: str
    status: str | None = None
    output_hash: str | None = None
    stderr_hash: str | None = None
    returncode: int | None = None
    truncated: bool = False
