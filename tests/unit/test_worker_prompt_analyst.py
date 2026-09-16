"""`WorkerAdapterPromptAnalyst` and `parse_prompt_analysis_output()` —
the real-adapter Prompt Analyst bridge (mirrors `test_engineering_
planning.py`'s "Planner protocol reuse" coverage of `WorkerAdapterPlanner`,
applied to prompt analysis)."""

from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.prompt_analysis import (
    AmbiguityRiskClass,
    hash_original_prompt,
    parse_prompt_analysis_output,
)
from code_slayer.workers.protocol import (
    WorkerAdapterError,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)
from code_slayer.workers.worker_prompt_analyst import (
    TOOL_NAME,
    PromptAnalystError,
    WorkerAdapterPromptAnalyst,
)

PROMPT = "Read README.md and summarize it."


def _tool_response(params):
    return WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL, tool_call=WorkerToolCall(tool=TOOL_NAME, params=params),
    )


# -- WorkerAdapterPromptAnalyst.analyze() ------------------------------------

def test_valid_structured_tool_call_becomes_real_analysis():
    params = {
        "goals": ["Summarize README.md"],
        "explicit_requirements": [],
        "constraints": [],
        "already_answered": [],
        "risk_points": [],
        "ambiguities": [],
    }
    adapter = FakeWorkerAdapter([_tool_response(params)])
    analyst = WorkerAdapterPromptAnalyst(adapter, task_id="analysis-x")
    analysis = analyst.analyze(PROMPT, {})
    assert analysis.original_prompt == PROMPT
    assert analysis.original_prompt_hash == hash_original_prompt(PROMPT)
    assert analysis.goals == ("Summarize README.md",)
    assert analysis.ambiguities == ()


def test_the_true_original_prompt_survives_even_though_the_wire_prompt_is_rendered():
    """The `WorkerRequest.original_prompt` actually sent over the wire is
    an instructional wrapper around the true prompt (mirrors `planning.
    worker_planner`'s own turn-rendering) -- but the returned
    `PromptAnalysis.original_prompt`/`.original_prompt_hash` must still
    be the exact, unmodified original text, never the rendered turn."""
    captured = []

    class _CapturingAdapter:
        def infer(self, request):
            captured.append(request.original_prompt)
            return _tool_response({"goals": []})

    analyst = WorkerAdapterPromptAnalyst(_CapturingAdapter(), task_id="analysis-x")
    analysis = analyst.analyze(PROMPT, {})
    assert captured[0] != PROMPT  # the wire prompt is the rendered turn, not the bare text
    assert PROMPT in captured[0]  # but it still carries the true text verbatim somewhere
    assert analysis.original_prompt == PROMPT
    assert analysis.original_prompt_hash == hash_original_prompt(PROMPT)


def test_ambiguities_round_trip_into_real_ambiguity_objects():
    params = {
        "ambiguities": [{
            "id": "scope", "question": "Which files?", "rationale": "Unclear scope.",
            "risk_class": "MATERIAL", "evidence_keys": ["repo:file_list"],
        }],
    }
    adapter = FakeWorkerAdapter([_tool_response(params)])
    analyst = WorkerAdapterPromptAnalyst(adapter, task_id="analysis-x")
    analysis = analyst.analyze(PROMPT, {})
    assert len(analysis.ambiguities) == 1
    ambiguity = analysis.ambiguities[0]
    assert ambiguity.id == "scope"
    assert ambiguity.risk_class == AmbiguityRiskClass.MATERIAL
    assert ambiguity.evidence_keys == ("repo:file_list",)


def test_text_response_raises_never_silently_becomes_an_empty_analysis():
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="Here is what I think you want..."),
    ])
    analyst = WorkerAdapterPromptAnalyst(adapter, task_id="analysis-x")
    try:
        analyst.analyze(PROMPT, {})
        raise AssertionError("expected PromptAnalystError")
    except PromptAnalystError as exc:
        assert "invalid_transport_response" in str(exc)


def test_textual_tool_protocol_leakage_is_never_recovered():
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="<tool_call>{}</tool_call>"),
    ])
    analyst = WorkerAdapterPromptAnalyst(adapter, task_id="analysis-x")
    try:
        analyst.analyze(PROMPT, {})
        raise AssertionError("expected PromptAnalystError")
    except PromptAnalystError as exc:
        assert "invalid_transport_response" in str(exc)


def test_a_tool_call_naming_anything_else_is_rejected():
    """`allowed_tools` is always exactly `(TOOL_NAME,)`, so `workers.
    protocol_validation.validate_response()` itself already rejects any
    other tool name as an unauthorized capability before this class's
    own (defensive, currently unreachable) tool-name check would ever
    run -- mirrors `test_worker_adapter_planner_rejects_wrong_tool_name`."""
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=WorkerToolCall(
            tool="read_file", params={"path": "README.md"},
        )),
    ])
    analyst = WorkerAdapterPromptAnalyst(adapter, task_id="analysis-x")
    try:
        analyst.analyze(PROMPT, {})
        raise AssertionError("expected PromptAnalystError")
    except PromptAnalystError as exc:
        assert "invalid_transport_response" in str(exc)


def test_transport_failure_is_never_swallowed():
    adapter = FakeWorkerAdapter([WorkerAdapterError("transport_timeout")])
    analyst = WorkerAdapterPromptAnalyst(adapter, task_id="analysis-x")
    try:
        analyst.analyze(PROMPT, {})
        raise AssertionError("expected PromptAnalystError")
    except PromptAnalystError as exc:
        assert "transport_timeout" in str(exc)


def test_genuine_tool_call_with_malformed_params_is_rejected_not_salvaged():
    adapter = FakeWorkerAdapter([_tool_response({"goals": ["x"], "unknown_field": 1})])
    analyst = WorkerAdapterPromptAnalyst(adapter, task_id="analysis-x")
    try:
        analyst.analyze(PROMPT, {})
        raise AssertionError("expected PromptAnalystError")
    except PromptAnalystError as exc:
        assert "malformed_structured_output" in str(exc)


# -- parse_prompt_analysis_output() ------------------------------------------

def test_parse_prompt_analysis_output_accepts_empty_object():
    fields = parse_prompt_analysis_output({})
    assert fields is not None
    assert fields.goals == ()
    assert fields.ambiguities == ()


def test_parse_prompt_analysis_output_rejects_unknown_field():
    assert parse_prompt_analysis_output({"goals": [], "not_a_real_field": 1}) is None


def test_parse_prompt_analysis_output_rejects_non_mapping():
    assert parse_prompt_analysis_output(["goals", "x"]) is None
    assert parse_prompt_analysis_output("goals") is None


def test_parse_prompt_analysis_output_rejects_wrong_shaped_string_list():
    assert parse_prompt_analysis_output({"goals": "not a list"}) is None
    assert parse_prompt_analysis_output({"goals": [1, 2]}) is None


def test_parse_prompt_analysis_output_rejects_malformed_ambiguity():
    assert parse_prompt_analysis_output({"ambiguities": [{"id": "x"}]}) is None
    assert parse_prompt_analysis_output(
        {"ambiguities": [{"id": "x", "question": "q", "rationale": "r", "risk_class": "BOGUS"}]},
    ) is None
