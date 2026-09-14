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
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponseKind,
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
        assert not CAPABILITIES[name].mutation


def test_tool_schemas_for_only_translates_known_read_only_capabilities(server):
    _script, base_url = server
    adapter = OpenAICompatibleAdapter(_config(base_url))
    schemas = adapter._tool_schemas_for(("read_file", "write_file", "made_up_tool"))
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {"read_file"}  # write_file and the invented name are silently omitted


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
