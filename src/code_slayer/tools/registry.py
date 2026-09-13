"""Closed capability registry. Registration is a code change, never request data."""

from dataclasses import dataclass
from types import MappingProxyType

from code_slayer.tools.models import RiskClass


@dataclass(frozen=True)
class Capability:
    name: str
    risk: RiskClass
    mutation: bool = False


CAPABILITIES = MappingProxyType({c.name: c for c in (
    Capability("read_file", RiskClass.READ_ONLY),
    Capability("create_file", RiskClass.WRITE_OWNED, True),
    Capability("write_file", RiskClass.WRITE_OWNED, True),
    Capability("apply_patch", RiskClass.WRITE_OWNED, True),
    Capability("run_command", RiskClass.GIT_READ),
)})
