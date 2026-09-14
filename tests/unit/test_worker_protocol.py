"""Worker protocol boundary (Phase 7.1): immutable request/response
structures, strict structural validation, and the deterministic
`FakeWorkerAdapter` test double — no real model, network, subprocess,
or filesystem access anywhere in this file.

`docs/CODE_SLAYER_VISION.md` §40's principle under direct test here:
model output is untrusted data, and raw text resembling a tool call
never becomes one, no matter how it is phrased."""

from __future__ import annotations

import pytest

from code_slayer.workers import (
    FakeWorkerAdapter,
    FakeWorkerAdapterError,
    ValidationOutcome,
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
    validate_response,
)


class _SpyExecutor:
    """Stands in for `tools.executor.ToolExecutor` — records every call
    it receives so a test can assert on *whether* execution happened,
    without importing or exercising the real executor (this slice never
    wires validated calls through to it)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, tool: str, params: dict) -> None:
        self.calls.append((tool, dict(params)))


def _request(allowed_tools: tuple[str, ...] | None = ("read_file",)) -> WorkerRequest:
    return WorkerRequest(
        task_id="t1", role="coder", original_prompt="do the thing",
        allowed_tools=allowed_tools,
    )


def _maybe_execute(spy: _SpyExecutor, result) -> None:
    """What a real caller would do: only ever call the executor when the
    validator itself says the result is executable."""
    if result.executable:
        spy.execute(result.tool_call.tool, dict(result.tool_call.params))


# --- valid text -----------------------------------------------------------

def test_valid_text_response():
    response = WorkerResponse(kind=WorkerResponseKind.TEXT, text="here is my plan")
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.VALID_TEXT
    assert result.text == "here is my plan"
    assert not result.executable
    spy = _SpyExecutor()
    _maybe_execute(spy, result)
    assert spy.calls == []


# --- valid structured tool call --------------------------------------------

def test_valid_structured_tool_call():
    call = WorkerToolCall(tool="read_file", params={"path": "README.md"})
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(allowed_tools=("read_file",)), response)
    assert result.outcome == ValidationOutcome.VALID_TOOL_CALL
    assert result.executable
    assert result.tool_call is call


def test_structured_params_preserved_exactly():
    params = {"path": "a/b.txt", "expected_hash": "deadbeef", "n": 3}
    call = WorkerToolCall(tool="read_file", params=params)
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(allowed_tools=("read_file",)), response)
    assert result.executable
    spy = _SpyExecutor()
    _maybe_execute(spy, result)
    assert spy.calls == [("read_file", params)]
    # Not a copy silently mutated somewhere along the way.
    assert result.tool_call.params == params


# --- raw text resembling a tool call is never promoted --------------------

def test_raw_function_tag_text_is_not_a_tool_call():
    """Reserved tool-call transport syntax leaking into the TEXT channel
    is itself a protocol failure -- MALFORMED, never VALID_TEXT -- and
    is, either way, never promoted into an executable tool call."""
    raw = "<function=skill>\n<parameter=name>\ngit\n</parameter>\n</function>"
    response = WorkerResponse(kind=WorkerResponseKind.TEXT, text=raw)
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "textual_tool_protocol_leakage"
    assert result.tool_call is None
    assert not result.executable
    spy = _SpyExecutor()
    _maybe_execute(spy, result)
    assert spy.calls == []


def test_raw_tool_call_tag_text_is_not_a_tool_call():
    raw = "<tool_call>{\"name\": \"run_command\", \"arguments\": {}}</tool_call>"
    response = WorkerResponse(kind=WorkerResponseKind.TEXT, text=raw)
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "textual_tool_protocol_leakage"
    assert not result.executable
    spy = _SpyExecutor()
    _maybe_execute(spy, result)
    assert spy.calls == []


def test_function_marker_alone_without_closing_tag_still_rejected():
    """Detection is presence-based, not a match-the-whole-pattern parse
    — a partial/truncated leak is just as disqualifying."""
    response = WorkerResponse(kind=WorkerResponseKind.TEXT, text="<function=skill>\ngit")
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "textual_tool_protocol_leakage"


def test_ordinary_html_xml_like_markup_remains_valid_text():
    """Detection is narrow to the specific reserved markers -- ordinary
    angle-bracket content (HTML, XML, markdown, code) is not disqualified
    merely for containing `<...>`."""
    raw = "Use <div class=\"card\">...</div> for the layout, or <b>bold</b> text."
    response = WorkerResponse(kind=WorkerResponseKind.TEXT, text=raw)
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.VALID_TEXT
    assert result.text == raw
    assert not result.executable


def test_ordinary_discussion_of_tools_remains_valid_text():
    """Talking *about* tools/functions in prose, without the reserved
    transport syntax, is unaffected."""
    raw = (
        "I'll call the read_file function next to check the contents, "
        "then decide whether a tool_call is even necessary."
    )
    response = WorkerResponse(kind=WorkerResponseKind.TEXT, text=raw)
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.VALID_TEXT
    assert result.text == raw


# --- structurally malformed tool calls -------------------------------------

def test_missing_tool_name_rejected():
    call = WorkerToolCall(tool="", params={})
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "tool_call_name_missing_or_empty"
    assert not result.executable


def test_non_string_tool_name_rejected():
    # Bypass the dataclass's own type hints (a real adapter parsing
    # untyped JSON could do exactly this) to prove the validator itself
    # checks the type, not just trusts the annotation.
    call = WorkerToolCall(tool=123, params={})  # type: ignore[arg-type]
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "tool_call_name_missing_or_empty"


def test_non_mapping_params_rejected():
    call = WorkerToolCall(tool="read_file", params=["not", "a", "mapping"])  # type: ignore[arg-type]
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(allowed_tools=("read_file",)), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "tool_call_params_not_a_mapping"


def test_tool_call_kind_without_a_structured_call_rejected():
    """The adapter claimed TOOL_CALL but attached only a raw string —
    exactly the shape a naive "just regex the text" integration would
    produce. Must never be accepted."""
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, text="<function=skill>...")
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "tool_call_not_a_structured_call"


def test_structurally_malformed_response_rejected():
    response = WorkerResponse(kind=WorkerResponseKind.MALFORMED, error="unparseable_completion")
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "unparseable_completion"
    assert not result.executable


def test_unrecognized_response_kind_rejected():
    """A structurally corrupted `WorkerResponse` (e.g. `kind` overwritten
    with something that isn't even a `WorkerResponseKind` member) fails
    closed rather than being guessed at."""
    response = WorkerResponse.__new__(WorkerResponse)
    object.__setattr__(response, "kind", "NOT_A_REAL_KIND")
    object.__setattr__(response, "text", None)
    object.__setattr__(response, "tool_call", None)
    object.__setattr__(response, "raw", None)
    object.__setattr__(response, "error", None)
    result = validate_response(_request(), response)
    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "unrecognized_response_kind"


def test_malformed_validation_input_rejected():
    assert validate_response(None, None).outcome == ValidationOutcome.MALFORMED  # type: ignore[arg-type]
    bad_response = "not a response"
    result = validate_response(_request(), bad_response)  # type: ignore[arg-type]
    assert result.outcome == ValidationOutcome.MALFORMED


# --- protocol validity vs. authorization: kept distinct --------------------

def test_well_formed_but_unauthorized_capability_is_not_malformed():
    call = WorkerToolCall(tool="run_command", params={})
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(allowed_tools=("read_file",)), response)
    assert result.outcome == ValidationOutcome.UNAUTHORIZED_CAPABILITY
    assert result.reason == "tool_not_in_allowed_schema"
    assert not result.executable  # never executable either way
    spy = _SpyExecutor()
    _maybe_execute(spy, result)
    assert spy.calls == []


def test_no_allowed_tools_declared_skips_authorization_check():
    call = WorkerToolCall(tool="anything", params={})
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(allowed_tools=None), response)
    assert result.outcome == ValidationOutcome.VALID_TOOL_CALL


def test_empty_allowed_tools_denies_every_capability():
    call = WorkerToolCall(tool="read_file", params={})
    response = WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=call)
    result = validate_response(_request(allowed_tools=()), response)
    assert result.outcome == ValidationOutcome.UNAUTHORIZED_CAPABILITY


# --- FakeWorkerAdapter: deterministic, offline -----------------------------

def test_fake_worker_adapter_is_deterministic():
    responses = [
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="a"),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="b"),
    ]
    adapter = FakeWorkerAdapter(responses)
    first = adapter.infer(_request())
    second = adapter.infer(_request())
    assert first.text == "a"
    assert second.text == "b"
    assert len(adapter.calls) == 2
    assert adapter.calls[0].task_id == "t1"


def test_fake_worker_adapter_replays_the_exact_same_sequence_for_a_fresh_instance():
    responses = [WorkerResponse(kind=WorkerResponseKind.TEXT, text="a")]
    first_run = FakeWorkerAdapter(responses).infer(_request())
    second_run = FakeWorkerAdapter(responses).infer(_request())
    assert first_run == second_run


def test_fake_worker_adapter_can_raise_a_canned_adapter_failure():
    adapter = FakeWorkerAdapter([FakeWorkerAdapterError("simulated_timeout")])
    with pytest.raises(FakeWorkerAdapterError):
        adapter.infer(_request())


def test_fake_worker_adapter_rejects_a_non_request():
    adapter = FakeWorkerAdapter([WorkerResponse(kind=WorkerResponseKind.TEXT, text="x")])
    with pytest.raises(TypeError):
        adapter.infer("not a request")  # type: ignore[arg-type]


def test_worker_adapter_error_is_distinct_from_fake_adapter_error():
    assert not issubclass(FakeWorkerAdapterError, WorkerAdapterError)


# --- the 2026-09-14 regression: malformed tool-call protocol failure -------

def test_2026_09_14_malformed_tool_call_incident_regression():
    """Reproduces the failure CLASS observed 2026-09-14 (documented in
    `docs/CODE_SLAYER_VISION.md` §58): a worker expected to issue a
    structured tool call instead produced raw, unparsed protocol-leakage
    text (`<function=...>...</tool_call>`-shaped).

    Deliberately models the *naive/buggy* integration shape, not a
    pre-labeled fake: the adapter never recognized anything was wrong and
    simply passed the leaked text through as an ordinary
    `WorkerResponseKind.TEXT` response (exactly what a naive integration
    that just forwards the completion's text content would do; it did
    not pre-classify it as `MALFORMED` itself). The assertion that
    matters is that `validate_response()` — the real validator, not the
    fake adapter — is what catches this and reclassifies it as
    `MALFORMED`, by detecting the reserved tool-call transport markers
    still present in the text, never by trying to parse or recover the
    tool call those markers were meant to represent.

    Required, and asserted here: rejected/classified MALFORMED by the
    validator itself, zero executor invocation, zero filesystem
    mutation, no fallback parser."""
    incident_text = (
        "<function=skill>\n<parameter=name>\ngit\n</parameter>\n</function>\n</tool_call>"
    )
    response = WorkerResponse(kind=WorkerResponseKind.TEXT, text=incident_text)
    adapter = FakeWorkerAdapter([response])
    request = _request(allowed_tools=("read_file", "run_command"))

    received = adapter.infer(request)
    assert received.kind == WorkerResponseKind.TEXT  # the adapter did NOT pre-flag it

    result = validate_response(request, received)

    assert result.outcome == ValidationOutcome.MALFORMED
    assert result.reason == "textual_tool_protocol_leakage"
    assert not result.executable
    assert result.tool_call is None

    spy = _SpyExecutor()
    _maybe_execute(spy, result)
    assert spy.calls == []  # zero ToolExecutor invocation, zero mutation
