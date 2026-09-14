"""A provider-neutral `WorkerAdapter` over any local OpenAI-compatible
chat-completions HTTP endpoint (Phase 7.4a).

This is the first real (non-fake) `WorkerAdapter` implementation. It
talks to whatever `OpenAICompatibleConfig.base_url` names — Ollama,
vLLM, llama.cpp's server, or anything else that speaks the same
`/chat/completions` shape — using only the Python standard library
(`urllib`), matching this codebase's zero-runtime-HTTP-dependency
posture (see `pyproject.toml`). No provider name, hostname, or model
name is hardcoded anywhere in this module: every concrete value comes
from the `OpenAICompatibleConfig` the caller supplies. A concrete
runtime/model combination (e.g. a specific Ollama host serving a
specific Qwen build) is something that constructs one of these and
registers a `worker_id` for it — never something this module knows
about by name.

## Network safety

This is intentional, explicitly-configured control-plane access to one
local model runtime — not a general network capability, and never
handed to the model itself:

- the endpoint is fixed at construction time; nothing in a
  `WorkerRequest` (and nothing a model could put in its own output) can
  change or redirect it
- every request goes through a `urllib` opener built with an *empty*
  proxy handler (`ProxyHandler({})`), so ambient `http_proxy`/
  `https_proxy`/`no_proxy` environment variables are never consulted —
  the connection goes directly to the configured host, always
- HTTP redirects are never followed — any 3xx response raises a
  `WorkerAdapterError` rather than silently reaching a different host
- a bounded connect/read timeout and a bounded maximum response size are
  always enforced
- only `Content-Type` and (if configured) a bearer `Authorization`
  header are ever sent — nothing from the calling process's own
  environment or credentials is forwarded
- there is no fallback to any other endpoint, local or cloud, ever
- the only tool schemas this module can ever emit come from its own
  fixed `_TOOL_SCHEMAS` table (currently just `read_file`) filtered by
  whatever the caller's `WorkerRequest.allowed_tools` actually allows —
  never a generic network/shell tool, never something the model asked
  for by name

## No raw tool-call recovery

If the model emits reserved tool-call transport syntax (`<function=...>`,
`<tool_call>`, ...) as plain assistant text instead of using the
provider's real structured `tool_calls` field, this adapter does exactly
what any other well-formed text response gets: it becomes
`WorkerResponse(kind=TEXT, text=...)`, verbatim, never inspected for
tool-call-shaped patterns here. `protocol_validation.validate_response()`
— already built, already tested in Phase 7.1 — is what recognizes that
leaked syntax and reclassifies the response `MALFORMED`. This module
never second-guesses a `TEXT` response and never attempts to parse,
repair, or "helpfully" recover a tool call from it.

## Deterministic generation and explicit tool requirement (Phase 7.4c)

The first live conformance runs against `qwen3-coder:30b` over Ollama's
OpenAI-compatible endpoint showed the *same* `structured_tool_call`
request sometimes returning a genuine structured `tool_calls` response
and sometimes returning raw `<function=...>`/`</tool_call>` text
instead — with generation left entirely uncontrolled (no `temperature`,
no `tool_choice`), this was structurally unreproducible evidence.
`OpenAICompatibleConfig.temperature` (default `0.0`) is the smallest
provider-neutral, standard OpenAI-compatible sampling control this
module adds — never a model-specific constant, and never something a
`WorkerRequest`/model output can change.

Separately, `WorkerRequest.tool_requirement` (`protocol.ToolRequirement`)
is mapped, only when `REQUIRED`, onto the standard OpenAI-compatible
`tool_choice: "required"` field. This is deliberately not derived from
`allowed_tools` being non-empty — an ordinary task may have tools
available without needing to use one — and it is deliberately not a
provider- or model-specific hack: `tool_choice: "required"` is standard
OpenAI-compatible request vocabulary, and this adapter maps it exactly
the same way regardless of which concrete runtime/model
`OpenAICompatibleConfig` happens to point at. Neither addition weakens
`protocol_validation.validate_response()` or this module's own no-
recovery contract above: a response that still arrives as leaked raw
text is still just `TEXT`, still never parsed here, and still only ever
reclassified by the validator.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RESPONSE_BYTES = 1_000_000  # 1 MiB — bounded, not unlimited
_DEFAULT_TEMPERATURE = 0.0  # deterministic by default — see the module docstring

# The complete, fixed set of tool schemas this adapter can ever translate
# a Code Slayer capability into. Deliberately not derived from
# tools.registry.CAPABILITIES wholesale: only capabilities this module
# has an explicit, reviewed JSON-schema translation for can ever be
# offered to a model, and today that is exactly one, read-only
# capability. Extending this table is a deliberate code change, never
# something a request or a model can do at runtime.
_TOOL_SCHEMAS: dict[str, dict] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of one file in the repository, identified "
                "by its path relative to the repository root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the repository root.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
}


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    """Everything the adapter needs, and nothing more. `base_url` and
    `model` are required and always explicit — there is no default that
    could silently point anywhere, and nothing here is ever populated
    from a `WorkerRequest` or model output.

    `temperature` defaults to `0.0` — deterministic generation, so
    conformance evidence is reproducible — rather than leaving sampling
    behavior at whatever the provider's own uncontrolled default is
    (see the module docstring's "Phase 7.4c" section). It is a standard
    OpenAI-compatible request field, generic across any provider this
    adapter talks to; `None` omits it from the request entirely, falling
    back to the provider's own default, for a caller that has a specific
    reason to want that instead."""

    base_url: str
    model: str
    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    api_key: str | None = field(default=None, repr=False)
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    temperature: float | None = _DEFAULT_TEMPERATURE

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url:
            raise ValueError("base_url must be a non-empty string")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model must be a non-empty string")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if not isinstance(self.max_response_bytes, int) or self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        if self.temperature is not None:
            if not isinstance(self.temperature, (int, float)):
                raise ValueError("temperature must be a number or None")
            if not (0.0 <= self.temperature <= 2.0):
                raise ValueError("temperature must be between 0.0 and 2.0")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect — a 3xx response becomes a definite
    transport failure, never a silent hop to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise urllib.error.HTTPError(newurl, code, "redirects are not followed", headers, fp)


class OpenAICompatibleAdapter:
    """`WorkerAdapter` over one configured OpenAI-compatible chat-
    completions endpoint. See the module docstring for the full network-
    safety and no-recovery contract."""

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        if not isinstance(config, OpenAICompatibleConfig):
            raise TypeError("OpenAICompatibleAdapter requires an OpenAICompatibleConfig")
        self._config = config
        # Built once: an empty ProxyHandler means ambient http_proxy/
        # https_proxy/no_proxy environment variables are never consulted,
        # and _NoRedirect means a 3xx response is always a hard failure.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        )

    def infer(self, request: WorkerRequest) -> WorkerResponse:
        if not isinstance(request, WorkerRequest):
            raise TypeError("OpenAICompatibleAdapter.infer() requires a WorkerRequest")
        payload = self._build_payload(request)
        body = self._post(payload)
        return self._to_worker_response(body)

    # -- request construction ------------------------------------------------

    def _build_payload(self, request: WorkerRequest) -> dict:
        messages = [{"role": "user", "content": request.original_prompt}]
        if request.prior_tool_result is not None:
            # Deliberately the smallest honest representation of "a tool
            # ran and here is what happened" — a plain follow-up user
            # message, not a fabricated assistant/tool message pair
            # standing in for conversation history this protocol does
            # not actually track (see workers.protocol.WorkerToolResult).
            messages.append({
                "role": "user",
                "content": (
                    f"(Result of the {request.prior_tool_result.tool} tool call: "
                    f"{request.prior_tool_result.output_summary})"
                ),
            })
        payload: dict = {"model": self._config.model, "messages": messages, "stream": False}
        if self._config.temperature is not None:
            payload["temperature"] = self._config.temperature
        tools = self._tool_schemas_for(request.allowed_tools)
        if tools:
            payload["tools"] = tools
        if request.tool_requirement == ToolRequirement.REQUIRED:
            if not tools:
                # A construction-time contract violation, not a transport
                # failure: nothing was actually offered to require use of.
                # Raised here, before any network call, rather than
                # silently sending a request that could never honor what
                # the caller asked for.
                raise ValueError(
                    "tool_requirement is REQUIRED but no tool schemas were resolved "
                    "from allowed_tools",
                )
            # Standard OpenAI-compatible vocabulary, mapped generically —
            # never a provider- or model-specific branch (see the module
            # docstring's "Phase 7.4c" section).
            payload["tool_choice"] = "required"
        return payload

    def _tool_schemas_for(self, allowed_tools: tuple[str, ...] | None) -> list[dict]:
        if not allowed_tools:
            return []
        # Any capability name this adapter has no known translation for
        # is silently omitted -- offering fewer tools than intended is a
        # functionality gap, never a safety issue, the same fail-closed
        # posture used throughout this codebase.
        return [_TOOL_SCHEMAS[name] for name in allowed_tools if name in _TOOL_SCHEMAS]

    # -- transport ------------------------------------------------------------

    def _post(self, payload: dict) -> dict:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=self._config.timeout) as resp:
                status = resp.status
                raw = resp.read(self._config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise WorkerAdapterError(f"http_error_{exc.code}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise WorkerAdapterError("transport_timeout") from exc
            raise WorkerAdapterError("transport_connection_failed") from exc
        except TimeoutError as exc:
            raise WorkerAdapterError("transport_timeout") from exc
        except OSError as exc:
            # Catch-all for any other low-level transport failure —
            # never let a raw socket/OS exception escape this boundary.
            raise WorkerAdapterError("transport_error") from exc
        if status != 200:
            raise WorkerAdapterError(f"http_status_{status}")
        if len(raw) > self._config.max_response_bytes:
            raise WorkerAdapterError("response_too_large")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerAdapterError("response_not_valid_json") from exc
        if not isinstance(body, dict):
            raise WorkerAdapterError("response_not_a_json_object")
        return body

    # -- response mapping -------------------------------------------------

    def _to_worker_response(self, body: dict) -> WorkerResponse:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return WorkerResponse(kind=WorkerResponseKind.MALFORMED, error="no_choices_in_response")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return WorkerResponse(kind=WorkerResponseKind.MALFORMED, error="no_message_in_choice")

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            # Bounded single-turn model: only the first tool call is
            # ever considered — no autonomous multi-call loop here.
            return self._first_tool_call_to_response(tool_calls[0])

        content = message.get("content")
        if isinstance(content, str):
            return WorkerResponse(kind=WorkerResponseKind.TEXT, text=content)

        return WorkerResponse(
            kind=WorkerResponseKind.MALFORMED, error="empty_message_no_content_no_tool_calls",
        )

    def _first_tool_call_to_response(self, tool_call: object) -> WorkerResponse:
        if not isinstance(tool_call, dict):
            return WorkerResponse(
                kind=WorkerResponseKind.MALFORMED, error="tool_call_not_an_object",
            )
        function = tool_call.get("function")
        if not isinstance(function, dict):
            return WorkerResponse(
                kind=WorkerResponseKind.MALFORMED, error="tool_call_missing_function",
            )
        name = function.get("name")
        raw_arguments = function.get("arguments")
        if not isinstance(name, str) or not name:
            return WorkerResponse(
                kind=WorkerResponseKind.MALFORMED, error="tool_call_name_missing_or_empty",
            )
        if not isinstance(raw_arguments, str):
            return WorkerResponse(
                kind=WorkerResponseKind.MALFORMED, error="tool_call_arguments_not_a_string",
            )
        try:
            params = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return WorkerResponse(
                kind=WorkerResponseKind.MALFORMED, error="tool_call_arguments_not_valid_json",
            )
        if not isinstance(params, dict):
            return WorkerResponse(
                kind=WorkerResponseKind.MALFORMED, error="tool_call_arguments_not_an_object",
            )
        return WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL, tool_call=WorkerToolCall(tool=name, params=params),
        )
