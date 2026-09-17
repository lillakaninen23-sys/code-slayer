"""Tool Protocol Compatibility Layer: deterministic normalization of an
explicitly-allowed textual pseudo-tool-call format into CSLR's canonical
internal `WorkerToolCall` — strictly BEFORE the existing strict
`workers.protocol_validation.validate_response()` boundary, never a
replacement for it (Tool Protocol Compatibility Layer foundation).

## Why this exists

`workers.protocol_validation.validate_response()` correctly classifies a
`TEXT` response containing reserved tool-call transport syntax (
`<function=`, `<tool_call>`, `</tool_call>`, `</function>`) as `MALFORMED`
with reason `"textual_tool_protocol_leakage"` — the model's structured
tool-calling channel did not fire, and the underlying model/provider
leaked raw protocol syntax into ordinary text instead. That boundary is
correct and stays completely unchanged (see that module's own "No
fallback parser" section): a `TEXT` response is never inspected for
content that merely resembles a tool call in some loose sense, and
`validate_response()` itself gains no new leniency of any kind.

Real-world testing against `qwen3-coder-ctx16k:30b` over an Ollama
OpenAI-compatible endpoint showed this exact leakage happening
reproducibly for an otherwise-correct planning response: the model
understood the task and produced the intended structured content, but
emitted it as literal `<function=...><parameter=...>...</parameter>
</function></tool_call>` text instead of a genuine transport-level
`tool_calls` response. Rejecting that outright (the existing, unchanged
behavior) is safe but wasteful when the underlying content is
genuinely, deterministically recoverable.

## What this layer is, and is not

This module is a strictly optional, explicitly-configured DECODER that
runs only when native transport already failed for this exact reason,
and only when the caller has explicitly enabled it for the exact
runtime profile in use. It is not:

- **A fallback parser for arbitrary malformed text.** A `ToolProtocol
  Normalizer` accepts only one exact, fully-specified grammar (see each
  concrete normalizer's own docstring, e.g. `workers.
  qwen_textual_tool_normalizer`) — never fuzzy-matched tags, never
  content extracted from surrounding prose, never inferred missing
  fields, never a "best effort" reconstruction of the model's probable
  intent. Any document that does not exactly match the accepted grammar
  is `REJECTED`, with the SAME fail-closed consequence as if no
  normalizer had run at all.
- **A weakening of `validate_response()`.** Nothing in this module
  changes what `validate_response()` accepts. A successful
  normalization produces a new, genuine `WorkerResponse(kind=TOOL_CALL,
  tool_call=...)` that is then run through the EXACT SAME
  `validate_response()` every native tool call already goes through —
  see `planning.worker_planner.WorkerAdapterPlanner.plan()` for the one
  production call site. `validate_response()` itself has no normalizer-
  aware code path at all.
- **A capability grant.** Producing a structurally valid
  `WorkerToolCall` here authorizes nothing — exactly like every other
  `WorkerToolCall` in this codebase, it still has to pass the existing
  `tool`/schema validator downstream (e.g. `planning.planner.
  parse_planner_output()`), and it never touches trust, permissions,
  policy, or certification. See `workers.role_qualification`/`workers.
  security_baseline` for why a runtime profile using a normalizer needs
  its own, separately-earned qualification/certification evidence
  rather than inheriting a native-only runtime's.
- **A global switch.** There is no "normalization always on" mode.
  Enabling one is an explicit, per-runtime-profile configuration
  decision an operator makes in code, never something a model response
  can request or declare about itself (a `WorkerResponse`/model output
  has no field this module ever reads to decide whether to normalize).

## Precedence: native transport always wins

A caller only ever attempts normalization after `validate_response()`
has already rejected the raw response for the SPECIFIC reason
`"textual_tool_protocol_leakage"` — never as a first resort, never for
any other rejection reason (a genuinely unauthorized capability, a
transport error, or ordinary non-tool prose are never routed through a
normalizer, since none of those are the "leaked textual protocol"
failure mode this layer exists to compensate for).

## Registry: explicit, code-owned, fail-closed resolution

`ToolProtocolNormalizerRegistry` holds every normalizer this codebase
actually implements, keyed by the exact `(normalizer_id,
normalizer_version)` pair each one declares. `resolve()` returns `None`
— never a best-effort nearest match — whenever no normalizer is
configured at all, or the configured `(id, version)` pair names one
this registry does not implement: an unknown normalizer id, a
recognized id at an unsupported version, and "no compatibility mode
configured for this runtime" all fail exactly the same way, closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from code_slayer.workers.protocol import WorkerToolCall

# Shared, layer-wide bound every compatibility decoder (and the
# provenance store that persists a successful decoder's original
# transport text) must honor. Distinct from, and much smaller than, the
# adapter-level transport ceiling
# (`workers.openai_compatible_adapter._DEFAULT_MAX_RESPONSE_BYTES`).
MAX_NORMALIZER_INPUT_CHARS = 65536


class NormalizationOutcome(StrEnum):
    """The complete, fixed outcome vocabulary one normalization attempt
    can produce. There is no partial/best-effort third state: a document
    either exactly matches a normalizer's accepted grammar and becomes a
    genuine `WorkerToolCall`, or it does not and is rejected outright."""

    NORMALIZED = "NORMALIZED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ToolProtocolNormalizationResult:
    """`tool_call` is populated only when `outcome == NORMALIZED`.
    `reason` is a short, stable, code-owned string — never raw model
    text — safe to use as durable provenance evidence (see `planning.
    planner_certification`/`planning.provenance` for how a caller
    records it)."""

    outcome: NormalizationOutcome
    reason: str
    tool_call: WorkerToolCall | None = None


class ToolProtocolNormalizer(Protocol):
    """The minimal interface every compatibility normalizer implements —
    structural (`typing.Protocol`), matching `workers.protocol.
    WorkerAdapter`/`workers.prompt_analysis.PromptAnalyst`'s own pattern.
    `normalizer_id`/`normalizer_version` are the exact, stable identity
    a `ToolProtocolNormalizerRegistry` keys registrations by, and the
    exact identity `workers.security_baseline.RuntimeProfileIdentity`
    binds a certificate to — see that module's own docstring for why a
    runtime profile using one normalizer/version must never be silently
    treated as equivalent to a different one, or to none at all.
    Identity attributes are read-only: a frozen normalizer dataclass is
    a valid implementation, and a caller must never mutate the identity
    a certificate is bound to."""

    @property
    def normalizer_id(self) -> str: ...

    @property
    def normalizer_version(self) -> int: ...

    def normalize(
        self,
        text: str,
        *,
        allowed_tools: tuple[str, ...],
    ) -> ToolProtocolNormalizationResult: ...


class ToolProtocolNormalizerRegistry:
    """The complete, code-owned set of normalizers this deployment
    actually implements. Never populated from configuration data, a
    model response, or anything other than an explicit Python-level
    registration at construction time — see the module docstring's
    "Registry" section for the fail-closed `resolve()` contract."""

    def __init__(self, normalizers: Sequence[ToolProtocolNormalizer] = ()) -> None:
        by_key: dict[tuple[str, int], ToolProtocolNormalizer] = {}
        for normalizer in normalizers:
            key = (normalizer.normalizer_id, normalizer.normalizer_version)
            if key in by_key:
                raise ValueError(f"duplicate normalizer registration: {key!r}")
            by_key[key] = normalizer
        self._by_key = by_key

    def resolve(
        self,
        normalizer_id: str | None,
        normalizer_version: int | None,
    ) -> ToolProtocolNormalizer | None:
        """`None` whenever `normalizer_id` is `None` (no compatibility
        normalizer configured for this runtime profile at all), or when
        the exact `(normalizer_id, normalizer_version)` pair does not
        name a normalizer this registry actually implements. Never falls
        back to a different version of the same id, and never guesses a
        default when `normalizer_version` is `None` but `normalizer_id`
        is not — an incomplete configuration fails closed exactly like a
        genuinely unknown one."""
        if normalizer_id is None or normalizer_version is None:
            return None
        return self._by_key.get((normalizer_id, normalizer_version))
