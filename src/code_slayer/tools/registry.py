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
    # Phase 5: durable Git checkpoint creation. Decided by its own narrow
    # policy function (`policy.engine.evaluate_checkpoint`), not the
    # path-shaped `PolicyEngine.evaluate()` above — see that function's
    # docstring. Registered here so the set of capabilities Code Slayer can
    # ever be asked to execute stays centrally enumerable and closed.
    Capability("checkpoint_create", RiskClass.GIT_MUTATION, True),
)})
