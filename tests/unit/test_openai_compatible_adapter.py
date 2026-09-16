"""Deterministic unit tests for `OpenAICompatibleAdapter` (Phase 7.4a).

Every test here runs against a real, local, ephemeral HTTP server bound
to `127.0.0.1` on an OS-assigned port — never the live Ollama endpoint
at `192.168.32.8`. `pytest` must never depend on that host being
reachable; the live smoke check is a separate, explicit action."""

from __future__ import annotations

import http.server
import json
import threading
import time
from dataclasses import fields

import pytest

from code_slayer.tools.registry import CAPABILITIES
from code_slayer.workers.openai_compatible_adapter import (
    _TOOL_SCHEMAS,
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
)
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponseKind,
    WorkerSupplementalKind,
    WorkerSupplementalResolution,
    WorkerSupplementalSource,
    WorkerToolResult,
)
from code_slayer.workers.protocol_validation import ValidationOutcome, validate_response


class _Script:
    """Mutable per-test response plan the fake server's handler reads
    from — set before calling `adapter.infer()`."""

    def __init__(self) -> None:
        self.status = 200
        self.body = b"{}"
        self.delay = 0.0
        self.extra_headers: dict[str, str] = {}
        self.last_request_body: bytes | None = None


def _make_handler(script: _Script) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if script.delay:
                time.sleep(script.delay)
            length = int(self.headers.get("Content-Length", 0))
            script.last_request_body = self.rfile.read(length)
            self.send_response(script.status)
            self.send_header("Content-Type", "application/json")
            for key, value in script.extra_headers.items():
                self.send_header(key, value)
            self.end_headers()
            if not self.wfile.closed:
                try:
                    self.wfile.write(script.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # keep test output clean

    return Handler


@pytest.fixture
def server():
    script = _Script()
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _make_handler(script))
    # A short poll_interval keeps shutdown() (called in teardown below)
    # fast -- the default 0.5s would otherwise add up across every test.
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
    )
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}/v1"
    try:
        yield script, base_url
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _config(base_url: str, **kwargs) -> OpenAICompatibleConfig:
    kwargs.setdefault("timeout", 5.0)
    return OpenAICompatibleConfig(base_url=base_url, model="test-model", **kwargs)


def _request(**kwargs) -> WorkerRequest:
    kwargs.setdefault("task_id", "t1")
    kwargs.setdefault("role", "coder")
    kwargs.setdefault("original_prompt", "hello")
    return WorkerRequest(**kwargs)


def _respond(script: _Script, message: dict) -> None:
    script.body = json.dumps({"choices": [{"message": message}]}).encode()


# --- 1/2/3. valid responses -------------------------------------------------

def test_valid_normal_text_response(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "Hello there"})
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.kind == WorkerResponseKind.TEXT
    assert response.text == "Hello there"


def test_valid_structured_tool_call(server):
    script, base_url = server
    _respond(script, {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "1", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
        }],
    })
    response = OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(allowed_tools=("read_file",)),
    )
    assert response.kind == WorkerResponseKind.TOOL_CALL
    assert response.tool_call.tool == "read_file"


def test_arguments_decode_to_structured_params(server):
    script, base_url = server
    _respond(script, {
        "role": "assistant",
        "tool_calls": [{
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "src/a.py", "expected_hash": "deadbeef"}),
            },
        }],
    })
    response = OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(allowed_tools=("read_file",)),
    )
    assert response.tool_call.params == {"path": "src/a.py", "expected_hash": "deadbeef"}


# --- 4. missing/invalid tool arguments fail closed --------------------------

def test_missing_tool_call_arguments_fails_closed(server):
    script, base_url = server
    _respond(script, {"tool_calls": [{"function": {"name": "read_file"}}]})  # no "arguments" at all
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.kind == WorkerResponseKind.MALFORMED
    assert response.error == "tool_call_arguments_not_a_string"


def test_non_json_tool_call_arguments_fail_closed(server):
    script, base_url = server
    _respond(script, {
        "tool_calls": [{"function": {"name": "read_file", "arguments": "{not json"}}],
    })
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.kind == WorkerResponseKind.MALFORMED
    assert response.error == "tool_call_arguments_not_valid_json"


def test_non_object_tool_call_arguments_fail_closed(server):
    script, base_url = server
    _respond(script, {"tool_calls": [{"function": {"name": "read_file", "arguments": "[1, 2]"}}]})
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.kind == WorkerResponseKind.MALFORMED
    assert response.error == "tool_call_arguments_not_an_object"


def test_missing_tool_call_name_fails_closed(server):
    script, base_url = server
    _respond(script, {"tool_calls": [{"function": {"arguments": "{}"}}]})
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.kind == WorkerResponseKind.MALFORMED
    assert response.error == "tool_call_name_missing_or_empty"


# --- 5. malformed provider response -----------------------------------------

def test_malformed_provider_response_no_choices(server):
    script, base_url = server
    script.body = json.dumps({"unexpected": "shape"}).encode()
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.kind == WorkerResponseKind.MALFORMED
    assert response.error == "no_choices_in_response"


def test_malformed_provider_response_empty_message(server):
    script, base_url = server
    _respond(script, {"role": "assistant"})  # no content, no tool_calls
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.kind == WorkerResponseKind.MALFORMED
    assert response.error == "empty_message_no_content_no_tool_calls"


# --- 6. raw textual protocol leakage remains TEXT, never recovered ---------

def test_raw_protocol_leakage_remains_text_until_validator_rejects_it(server):
    """The exact real-world regression check: the adapter itself must
    never parse or recover this -- it stays TEXT, verbatim, and only the
    already-existing Phase 7.1 validator reclassifies it MALFORMED."""
    script, base_url = server
    leaked = "<function=skill>\n<parameter=name>\ngit\n</parameter>\n</function>"
    _respond(script, {"role": "assistant", "content": leaked})
    request = _request()
    response = OpenAICompatibleAdapter(_config(base_url)).infer(request)
    assert response.kind == WorkerResponseKind.TEXT
    assert response.text == leaked  # verbatim, not parsed or altered

    result = validate_response(request, response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "textual_tool_protocol_leakage"


# --- 7/8/9. transport failures all become WorkerAdapterError ---------------

def test_http_timeout_raises_worker_adapter_error(server):
    script, base_url = server
    script.delay = 0.4  # the server's handler thread still owns this delay
    adapter = OpenAICompatibleAdapter(_config(base_url, timeout=0.05))
    with pytest.raises(WorkerAdapterError):
        adapter.infer(_request())


def test_connection_failure_raises_worker_adapter_error():
    # Port 1: nothing listens there; a client connection is refused fast.
    adapter = OpenAICompatibleAdapter(_config("http://127.0.0.1:1/v1", timeout=2.0))
    with pytest.raises(WorkerAdapterError):
        adapter.infer(_request())


def test_non_success_http_status_raises_worker_adapter_error(server):
    script, base_url = server
    script.status = 500
    script.body = json.dumps({"error": "internal"}).encode()
    adapter = OpenAICompatibleAdapter(_config(base_url))
    with pytest.raises(WorkerAdapterError):
        adapter.infer(_request())


def test_redirect_is_never_followed(server):
    script, base_url = server
    script.status = 302
    script.extra_headers = {"Location": "http://example.invalid/somewhere"}
    script.body = b""
    adapter = OpenAICompatibleAdapter(_config(base_url))
    with pytest.raises(WorkerAdapterError):
        adapter.infer(_request())


# --- 10. response size / malformed JSON -------------------------------------

def test_oversized_response_raises_worker_adapter_error(server):
    script, base_url = server
    huge = "x" * 2_000_000
    script.body = json.dumps({"choices": [{"message": {"content": huge}}]}).encode()
    adapter = OpenAICompatibleAdapter(_config(base_url, max_response_bytes=1000))
    with pytest.raises(WorkerAdapterError):
        adapter.infer(_request())


def test_malformed_json_response_raises_worker_adapter_error(server):
    script, base_url = server
    script.body = b"{not valid json"
    adapter = OpenAICompatibleAdapter(_config(base_url))
    with pytest.raises(WorkerAdapterError):
        adapter.infer(_request())


# --- 11. endpoint is configuration-owned, never model-owned -----------------

def test_worker_request_has_no_endpoint_controlling_field():
    field_names = {f.name for f in fields(WorkerRequest)}
    assert field_names == {
        "task_id", "role", "original_prompt", "allowed_tools", "prior_tool_result",
        "tool_requirement", "supplemental_resolutions", "max_output_tokens",
    }


def test_config_is_immutable_after_construction(server):
    _script, base_url = server
    config = _config(base_url)
    with pytest.raises(Exception):  # noqa: B017 -- frozen dataclass raises FrozenInstanceError
        config.base_url = "http://evil.example/v1"


def test_ambient_proxy_env_vars_are_never_consulted(server, monkeypatch):
    """If the adapter obeyed http_proxy, this request would route
    through a nonexistent proxy and fail; succeeding proves it always
    goes directly to the configured base_url instead."""
    script, base_url = server
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1/")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1/")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1/")
    _respond(script, {"role": "assistant", "content": "direct connection worked"})
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.text == "direct connection worked"


# --- 12. no mutating tool schema exists, at all ------------------------------

def test_no_mutating_capability_has_a_tool_schema():
    for name in _TOOL_SCHEMAS:
        if name not in CAPABILITIES:
            # Not every `_TOOL_SCHEMAS` entry is a real ToolExecutor
            # capability at all -- see the next test.
            continue
        assert not CAPABILITIES[name].mutation


def test_structured_output_only_schemas_are_not_registered_capabilities():
    """`emit_engineering_plan` (Phase 8.2/8.2b, `planning.
    worker_planner.WorkerAdapterPlanner`) is a structured-output
    transport tool only -- it must never collide with, or be mistaken
    for, a real `tools.registry.CAPABILITIES` entry `ToolExecutor` could
    route to. Offering its schema grants no filesystem mutation, no
    command execution, no checkpoint authority, and no trust promotion:
    nothing in this module imports `tools.executor.ToolExecutor`,
    `policy.engine.PolicyEngine`, `repo.checkpoint`, or `workers.trust`
    promotion at all."""
    assert "emit_engineering_plan" in _TOOL_SCHEMAS
    assert "emit_engineering_plan" not in CAPABILITIES


def test_tool_schemas_for_only_translates_known_read_only_capabilities(server):
    _script, base_url = server
    adapter = OpenAICompatibleAdapter(_config(base_url))
    schemas = adapter._tool_schemas_for(("read_file", "write_file", "made_up_tool"))
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {"read_file"}  # write_file and the invented name are silently omitted


# --- Phase 8.2b: emit_engineering_plan schema, offered only when allowed ----

def test_emit_engineering_plan_schema_offered_when_explicitly_allowed(server):
    script, base_url = server
    _respond(script, {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "function": {"name": "emit_engineering_plan", "arguments": json.dumps({"goal": "x"})},
        }],
    })
    OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(allowed_tools=("emit_engineering_plan",)),
    )
    sent = json.loads(script.last_request_body)
    names = {tool["function"]["name"] for tool in sent["tools"]}
    assert names == {"emit_engineering_plan"}


def test_emit_engineering_plan_schema_absent_when_not_allowed(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    OpenAICompatibleAdapter(_config(base_url)).infer(_request(allowed_tools=("read_file",)))
    sent = json.loads(script.last_request_body)
    names = {tool["function"]["name"] for tool in sent["tools"]}
    assert "emit_engineering_plan" not in names

    OpenAICompatibleAdapter(_config(base_url)).infer(_request(allowed_tools=None))
    sent = json.loads(script.last_request_body)
    assert "tools" not in sent  # no allowed_tools at all -> nothing offered


def test_emit_engineering_plan_required_emits_standard_tool_choice(server):
    """Test item 10: exactly the same standard `tool_choice: "required"`
    mapping `test_required_tool_requirement_emits_standard_tool_choice_
    field` proves for `read_file` above, exercised for
    `emit_engineering_plan` specifically -- no special-cased branch for
    this tool name anywhere in `_build_payload()`."""
    script, base_url = server
    _respond(script, {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "function": {"name": "emit_engineering_plan", "arguments": json.dumps({"goal": "x"})},
        }],
    })
    OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(
            allowed_tools=("emit_engineering_plan",), tool_requirement=ToolRequirement.REQUIRED,
        ),
    )
    sent = json.loads(script.last_request_body)
    assert sent["tool_choice"] == "required"
    names = {tool["function"]["name"] for tool in sent["tools"]}
    assert names == {"emit_engineering_plan"}


def test_no_tools_sent_when_request_allows_none(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    OpenAICompatibleAdapter(_config(base_url)).infer(_request(allowed_tools=None))
    sent = json.loads(script.last_request_body)
    assert "tools" not in sent


# --- 13. tool-result continuation round trip --------------------------------

def test_tool_result_continuation_round_trip(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "Continuing after the tool result."})
    prior = WorkerToolResult(tool="read_file", output_summary="file contains: hello world")
    response = OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(prior_tool_result=prior),
    )
    assert response.kind == WorkerResponseKind.TEXT
    sent = json.loads(script.last_request_body)
    assert any("hello world" in m.get("content", "") for m in sent["messages"])


# --- Phase 7.4c: deterministic generation ------------------------------------

def test_default_temperature_is_zero_and_sent_in_payload(server):
    """Test item 4: deterministic generation options are encoded as
    intended -- the default is 0.0 (deterministic), sent as a standard
    OpenAI-compatible field, not left to the provider's own default."""
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    sent = json.loads(script.last_request_body)
    assert sent["temperature"] == 0.0


def test_temperature_none_omits_the_field(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    OpenAICompatibleAdapter(_config(base_url, temperature=None)).infer(_request())
    sent = json.loads(script.last_request_body)
    assert "temperature" not in sent


def test_custom_temperature_is_sent_verbatim(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    OpenAICompatibleAdapter(_config(base_url, temperature=0.5)).infer(_request())
    sent = json.loads(script.last_request_body)
    assert sent["temperature"] == 0.5


def test_out_of_range_temperature_rejected_at_construction():
    # No live connection is ever attempted -- construction itself fails.
    unreachable = "http://127.0.0.1:1/v1"
    with pytest.raises(ValueError):
        _config(unreachable, temperature=2.5)
    with pytest.raises(ValueError):
        _config(unreachable, temperature=-0.1)


# --- Phase 7.4c: tool_requirement / tool_choice ------------------------------

def test_normal_request_does_not_globally_require_a_tool(server):
    """Test item 1: a request with tools available but the default
    OPTIONAL tool_requirement never sends tool_choice at all -- having
    tools available never, by itself, demands using one."""
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok, no tool needed"})
    OpenAICompatibleAdapter(_config(base_url)).infer(_request(allowed_tools=("read_file",)))
    sent = json.loads(script.last_request_body)
    assert "tool_choice" not in sent
    assert "tools" in sent  # the tool was still offered, just not required


def test_required_tool_requirement_emits_standard_tool_choice_field(server):
    """Test items 2/3: an explicit REQUIRED tool_requirement is mapped to
    the standard OpenAI-compatible tool_choice field."""
    script, base_url = server
    _respond(script, {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
        }],
    })
    OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(allowed_tools=("read_file",), tool_requirement=ToolRequirement.REQUIRED),
    )
    sent = json.loads(script.last_request_body)
    assert sent["tool_choice"] == "required"


def test_required_tool_requirement_with_no_resolvable_tools_raises(server):
    """A construction-time contract violation, never silently sent:
    REQUIRED with nothing actually offered to require use of."""
    _script, base_url = server
    adapter = OpenAICompatibleAdapter(_config(base_url))
    with pytest.raises(ValueError):
        adapter.infer(_request(allowed_tools=None, tool_requirement=ToolRequirement.REQUIRED))
    with pytest.raises(ValueError):
        adapter.infer(_request(allowed_tools=(), tool_requirement=ToolRequirement.REQUIRED))


def test_valid_structured_tool_call_still_maps_correctly_with_new_fields(server):
    """Test item 5: valid structured tool_calls still map correctly with
    deterministic generation and an explicit tool requirement both set."""
    script, base_url = server
    _respond(script, {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "function": {"name": "read_file", "arguments": json.dumps({"path": "b.txt"})},
        }],
    })
    response = OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(allowed_tools=("read_file",), tool_requirement=ToolRequirement.REQUIRED),
    )
    assert response.kind == WorkerResponseKind.TOOL_CALL
    assert response.tool_call.tool == "read_file"
    assert response.tool_call.params == {"path": "b.txt"}


def test_textual_function_leakage_remains_malformed_with_required_tool_requirement(server):
    """Test item 6: even with tool_choice=required sent, a provider that
    still leaks raw textual protocol syntax is unaffected by this
    module -- it stays TEXT, verbatim, and only validate_response()
    reclassifies it MALFORMED (test item 7: no fallback parser exists
    here, regardless of tool_requirement)."""
    script, base_url = server
    leaked = "<function=read_file>\n<parameter=path>\nREADME.md\n</parameter>\n</function>"
    _respond(script, {"role": "assistant", "content": leaked})
    request = _request(allowed_tools=("read_file",), tool_requirement=ToolRequirement.REQUIRED)
    response = OpenAICompatibleAdapter(_config(base_url)).infer(request)
    assert response.kind == WorkerResponseKind.TEXT
    assert response.text == leaked

    result = validate_response(request, response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "textual_tool_protocol_leakage"


# --- Phase 7.7d: supplemental resolutions rendered deterministically -------

def test_supplemental_resolution_rendered_as_separate_labeled_message(server):
    """Item 22: the original prompt message is untouched, and each
    supplemental resolution becomes its own clearly-labeled message --
    never merged into, or mistaken for, the user's own original text."""
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    resolution = WorkerSupplementalResolution(
        ambiguity_id="target-module", kind=WorkerSupplementalKind.FACT,
        source=WorkerSupplementalSource.DURABLE_TASK_EVIDENCE,
        content="billing.py", content_hash="deadbeef",
    )
    request = _request(
        original_prompt="Refactor the ambiguous module.",
        supplemental_resolutions=(resolution,),
    )
    OpenAICompatibleAdapter(_config(base_url)).infer(request)
    sent = json.loads(script.last_request_body)
    messages = sent["messages"]
    assert messages[0] == {"role": "user", "content": "Refactor the ambiguous module."}
    assert len(messages) == 2
    assert messages[1]["role"] == "user"
    assert "billing.py" in messages[1]["content"]
    assert "target-module" in messages[1]["content"]
    assert "FACT" in messages[1]["content"]
    assert messages[1]["content"] != "billing.py"  # never bare, unlabeled text


def test_multiple_supplemental_resolutions_render_in_given_order(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    first = WorkerSupplementalResolution(
        ambiguity_id="a", kind=WorkerSupplementalKind.FACT,
        source=WorkerSupplementalSource.DURABLE_TASK_EVIDENCE,
        content="first answer", content_hash="h1",
    )
    second = WorkerSupplementalResolution(
        ambiguity_id="b", kind=WorkerSupplementalKind.AUTHORIZATION,
        source=WorkerSupplementalSource.ORIGINAL_PROMPT,
        content="yes, authorized", content_hash="h2",
    )
    request = _request(supplemental_resolutions=(first, second))
    OpenAICompatibleAdapter(_config(base_url)).infer(request)
    sent = json.loads(script.last_request_body)
    messages = sent["messages"]
    assert "first answer" in messages[1]["content"]
    assert "AUTHORIZATION" in messages[2]["content"]
    assert "yes, authorized" in messages[2]["content"]


def test_supplemental_resolutions_precede_prior_tool_result_message(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    resolution = WorkerSupplementalResolution(
        ambiguity_id="a", kind=WorkerSupplementalKind.FACT,
        source=WorkerSupplementalSource.DURABLE_TASK_EVIDENCE,
        content="the answer", content_hash="h1",
    )
    prior = WorkerToolResult(tool="read_file", output_summary="file contents")
    request = _request(supplemental_resolutions=(resolution,), prior_tool_result=prior)
    OpenAICompatibleAdapter(_config(base_url)).infer(request)
    sent = json.loads(script.last_request_body)
    messages = sent["messages"]
    assert len(messages) == 3
    assert "the answer" in messages[1]["content"]
    assert "file contents" in messages[2]["content"]


# --- max_output_tokens / usage (Phase 8.2e context-adequacy hardening) -----


def test_max_output_tokens_is_omitted_by_default(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    sent = json.loads(script.last_request_body)
    assert "max_tokens" not in sent


def test_max_output_tokens_maps_to_standard_max_tokens_field(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    OpenAICompatibleAdapter(_config(base_url)).infer(_request(max_output_tokens=256))
    sent = json.loads(script.last_request_body)
    assert sent["max_tokens"] == 256


def test_max_output_tokens_must_be_a_positive_integer(server):
    _script, base_url = server
    with pytest.raises(ValueError):
        OpenAICompatibleAdapter(_config(base_url)).infer(_request(max_output_tokens=0))
    with pytest.raises(ValueError):
        OpenAICompatibleAdapter(_config(base_url)).infer(_request(max_output_tokens=-5))


def test_usage_is_parsed_and_attached_to_the_response(server):
    script, base_url = server
    script.body = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }).encode()
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.usage is not None
    assert response.usage.prompt_tokens == 100
    assert response.usage.completion_tokens == 20
    assert response.usage.total_tokens == 120


def test_usage_is_attached_for_a_tool_call_response_too(server):
    script, base_url = server
    script.body = json.dumps({
        "choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a"}'},
            }],
        }}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    }).encode()
    response = OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(allowed_tools=("read_file",), tool_requirement=ToolRequirement.REQUIRED),
    )
    assert response.kind == WorkerResponseKind.TOOL_CALL
    assert response.usage is not None
    assert response.usage.total_tokens == 55


def test_usage_is_none_when_the_provider_does_not_report_it(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.usage is None


def test_malformed_usage_is_never_partially_reconstructed(server):
    script, base_url = server
    script.body = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 100},  # missing completion_tokens/total_tokens
    }).encode()
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.usage is None


def test_finish_reason_is_threaded_through_for_text_responses(server):
    script, base_url = server
    script.body = json.dumps({
        "choices": [{
            "message": {"role": "assistant", "content": "ok"}, "finish_reason": "length",
        }],
    }).encode()
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.finish_reason == "length"


def test_finish_reason_is_threaded_through_for_tool_call_responses(server):
    script, base_url = server
    script.body = json.dumps({
        "choices": [{
            "message": {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }).encode()
    response = OpenAICompatibleAdapter(_config(base_url)).infer(
        _request(allowed_tools=("read_file",), tool_requirement=ToolRequirement.REQUIRED),
    )
    assert response.finish_reason == "tool_calls"


def test_finish_reason_is_none_when_the_provider_does_not_report_it(server):
    script, base_url = server
    _respond(script, {"role": "assistant", "content": "ok"})
    response = OpenAICompatibleAdapter(_config(base_url)).infer(_request())
    assert response.finish_reason is None
