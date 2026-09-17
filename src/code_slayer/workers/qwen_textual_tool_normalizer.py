"""`qwen_textual_tool_v1`: the first concrete `workers.
protocol_normalization.ToolProtocolNormalizer` — a strict, deterministic
decoder for the exact textual pseudo-tool-call shape observed from
`qwen3-coder-ctx16k:30b` over an Ollama OpenAI-compatible endpoint
(Tool Protocol Compatibility Layer foundation).

## Exact accepted grammar

Only a document structurally equivalent to this exact real-world-
observed shape is ever accepted:

```
<function=NAME>
<parameter=PARAM_1>
VALUE_1
</parameter>
<parameter=PARAM_2>
VALUE_2
</parameter>
</function>
```

An optional trailing `</tool_call>` closer with no matching opening
`<tool_call>` is also accepted — that exact asymmetry is the 2026-09-14
leak shape (`docs/CODE_SLAYER_VISION.md` §58) and the live
`qwen3-coder-ctx16k:30b`/Ollama observation this decoder was written
against. A document that *opens* with `<tool_call>` does not match this
grammar and is rejected.

- exactly one `<function=NAME>` ... `</function>` invocation in the
  whole document (a second, anywhere — even nested inside a parameter
  value — rejects the entire document)
- `NAME` must be a valid identifier (`[A-Za-z_][A-Za-z0-9_]*`) and must
  exactly equal one of `allowed_tools` — an unrecognized or spoofed
  name rejects
- no non-whitespace content before `<function=` or after `</function>`
  (or after the optional trailing `</tool_call>` when present)
- zero or more `<parameter=NAME>VALUE</parameter>` blocks between the
  function open/close tags, each with a valid identifier name
- a duplicate parameter name, an unrecognized parameter name (checked
  against the caller-supplied `known_parameters`), a parameter value
  containing any reserved tag marker (a nested/recursive pseudo-tool
  invocation), or any tag that never closes all reject the whole
  document
- every parameter value NOT in `string_parameters` must parse as valid
  JSON once trimmed of surrounding whitespace — malformed JSON,
  non-finite constants (`NaN`/`Infinity`), and duplicate object keys
  all reject

There is no recovery, no fuzzy matching, no extraction from surrounding
prose, and no inference of a missing field: a document either exactly
matches this grammar or the whole attempt is `REJECTED`. This decoder
never invokes a model, never uses `re`-based "find something that looks
like a tag" scanning, and never uses `eval`/`exec` — every character is
consumed by explicit, bounded string operations only.

## Not a schema validator

`known_parameters`/`string_parameters` exist only to keep this decoder
from accepting an obviously-wrong document (an unrecognized parameter
name, or a value that was supposed to be JSON but is not) — this is NOT
a replacement for `planning.planner.parse_planner_output()`'s own strict
schema check (required fields, per-field types, structural shape of
each JSON value). A successfully normalized `WorkerToolCall` still has
to pass that existing validator, completely unchanged, exactly like a
genuine native tool call does.

## No authority

Producing a `WorkerToolCall` here authorizes nothing: this module has no
`ToolExecutor`, `PolicyEngine`, trust, or certification import anywhere,
and never will — see `workers.protocol_normalization`'s own module
docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from code_slayer.workers.protocol import WorkerToolCall
from code_slayer.workers.protocol_normalization import (
    NormalizationOutcome,
    ToolProtocolNormalizationResult,
)

NORMALIZER_ID = "qwen_textual_tool_v1"
NORMALIZER_VERSION = 1

# A conservative, deterministic bound distinct from (and much smaller
# than) `workers.openai_compatible_adapter._DEFAULT_MAX_RESPONSE_BYTES`
# (1 MiB) -- an engineering-plan-shaped textual pseudo-tool-call has no
# legitimate reason to approach that ceiling, and bounding parsing cost
# here is a deliberate, separate defense, not a reuse of that adapter-
# level transport limit.
_MAX_INPUT_CHARS = 65536

_FUNCTION_OPEN = "<function="
_FUNCTION_CLOSE = "</function>"
_PARAMETER_OPEN = "<parameter="
_PARAMETER_CLOSE = "</parameter>"
_TOOL_CALL_OPEN = "<tool_call>"
_TOOL_CALL_CLOSE = "</tool_call>"

# The complete reserved-marker set a parameter VALUE must never contain
# -- mirrors `workers.protocol_validation._RESERVED_TOOL_PROTOCOL_
# MARKERS` exactly (duplicated as literals rather than imported, since
# that name is private to a sibling module); any of these appearing
# inside a value means a nested/recursive pseudo-tool invocation, or a
# tag that was never meant to be inside a value at all -- rejected
# either way, never guessed apart.
_RESERVED_MARKERS = (
    _FUNCTION_OPEN,
    _FUNCTION_CLOSE,
    _PARAMETER_OPEN,
    _PARAMETER_CLOSE,
    _TOOL_CALL_OPEN,
    _TOOL_CALL_CLOSE,
)


def _is_valid_identifier(value: str) -> bool:
    """A deterministic, non-regex identifier check -- no ambiguity, no
    partial match: every character must be alphanumeric or `_`, and the
    first must not be a digit."""
    if not value:
        return False
    first = value[0]
    if not (first.isalpha() or first == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in value)


def _reject(reason: str) -> ToolProtocolNormalizationResult:
    return ToolProtocolNormalizationResult(NormalizationOutcome.REJECTED, reason)


def _strict_json_loads(raw: str) -> object:
    """`json.loads` with the extra fail-closed constraints this decoder
    actually needs: no `NaN`/`Infinity`, no duplicate object keys. Never
    a second, heuristic JSON repair pass."""

    def _reject_constant(value: str) -> None:
        raise json.JSONDecodeError(f"non_finite_constant:{value}", raw, 0)

    def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise json.JSONDecodeError("duplicate_object_key", raw, 0)
            result[key] = value
        return result

    return json.loads(
        raw,
        parse_constant=_reject_constant,
        object_pairs_hook=_no_duplicate_keys,
    )


@dataclass(frozen=True)
class QwenTextualToolNormalizer:
    """`known_parameters` is the complete set of parameter names this
    normalizer will ever accept for the tool it decodes calls for —
    supplied by the caller, never hardcoded here, so this class carries
    no Planner-specific (or any other tool-specific) knowledge of its
    own (see the module docstring). The one production wiring,
    `planning.worker_planner.build_default_normalizer_registry()`,
    constructs this with `planning.planner.STRUCTURED_OUTPUT_FIELDS` —
    the exact same field-name authority `parse_planner_output()` itself
    enforces downstream — as `known_parameters`, so "inspect the actual
    tool schema and use it as authority" is satisfied by construction,
    not duplicated inline here.

    `string_parameters` names the subset of `known_parameters` whose
    value is plain text rather than a JSON-encoded value (e.g.
    `emit_engineering_plan`'s own `goal` field) — every OTHER known
    parameter's value must parse as JSON."""

    known_parameters: frozenset[str]
    string_parameters: frozenset[str]
    normalizer_id: str = NORMALIZER_ID
    normalizer_version: int = NORMALIZER_VERSION

    def normalize(
        self,
        text: str,
        *,
        allowed_tools: tuple[str, ...],
    ) -> ToolProtocolNormalizationResult:
        if not isinstance(text, str):
            return _reject("not_a_string")
        if len(text) > _MAX_INPUT_CHARS:
            return _reject("input_too_large")

        # Exactly one function invocation, no tool_call OPEN marker, and
        # at most one tool_call close marker -- the exact, asymmetric
        # shape this grammar accepts (see the module docstring). A
        # second occurrence of any of these, anywhere in the document
        # including nested inside a parameter value, rejects the whole
        # document immediately -- this single set of counts is what
        # makes "multiple functions" and "nested/recursive pseudo-tool
        # invocation" both fail closed before any detailed parsing even
        # begins.
        if text.count(_FUNCTION_OPEN) != 1:
            return _reject("expected_exactly_one_function_invocation")
        if text.count(_FUNCTION_CLOSE) != 1:
            return _reject("expected_exactly_one_function_close_tag")
        if text.count(_TOOL_CALL_OPEN) != 0:
            return _reject("unexpected_tool_call_open_tag")
        close_count = text.count(_TOOL_CALL_CLOSE)
        if close_count > 1:
            return _reject("expected_at_most_one_tool_call_close_tag")

        function_open_index = text.index(_FUNCTION_OPEN)
        if text[:function_open_index].strip():
            return _reject("non_whitespace_prefix")

        name_start = function_open_index + len(_FUNCTION_OPEN)
        name_end = text.find(">", name_start)
        if name_end == -1:
            return _reject("malformed_function_open_tag")
        function_name = text[name_start:name_end]
        if not _is_valid_identifier(function_name):
            return _reject("malformed_function_name")
        if function_name not in allowed_tools:
            return _reject("unknown_function_name")

        function_close_index = text.index(_FUNCTION_CLOSE)
        if function_close_index < name_end:
            return _reject("malformed_document_structure")
        body = text[name_end + 1 : function_close_index]

        params: dict[str, object] = {}
        seen: set[str] = set()
        remaining = body
        while remaining.strip():
            lstripped = remaining.lstrip()
            if not lstripped.startswith(_PARAMETER_OPEN):
                return _reject("malformed_function_body")
            offset = len(remaining) - len(lstripped)
            param_name_start = offset + len(_PARAMETER_OPEN)
            param_name_end = remaining.find(">", param_name_start)
            if param_name_end == -1:
                return _reject("malformed_parameter_open_tag")
            param_name = remaining[param_name_start:param_name_end]
            if not _is_valid_identifier(param_name):
                return _reject("malformed_parameter_name")

            value_start = param_name_end + 1
            value_end = remaining.find(_PARAMETER_CLOSE, value_start)
            if value_end == -1:
                return _reject("missing_parameter_close_tag")
            raw_value = remaining[value_start:value_end]
            if any(marker in raw_value for marker in _RESERVED_MARKERS):
                return _reject("nested_pseudo_tool_content")

            if param_name in seen:
                return _reject("duplicate_parameter")
            if param_name not in self.known_parameters:
                return _reject("unknown_parameter")
            seen.add(param_name)

            if param_name in self.string_parameters:
                params[param_name] = raw_value.strip()
            else:
                try:
                    params[param_name] = _strict_json_loads(raw_value.strip())
                except json.JSONDecodeError:
                    return _reject("malformed_json_parameter_value")

            remaining = remaining[value_end + len(_PARAMETER_CLOSE) :]

        after_function = text[function_close_index + len(_FUNCTION_CLOSE) :]
        trailer = after_function.strip()
        if close_count == 0:
            if trailer:
                return _reject("non_whitespace_suffix")
        elif trailer != _TOOL_CALL_CLOSE:
            return _reject("non_whitespace_suffix")

        return ToolProtocolNormalizationResult(
            NormalizationOutcome.NORMALIZED,
            "qwen_textual_tool_v1_normalized",
            tool_call=WorkerToolCall(tool=function_name, params=params),
        )
