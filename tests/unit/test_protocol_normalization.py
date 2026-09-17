"""Tool Protocol Compatibility Layer: strict deterministic decoder
(`workers.protocol_normalization` / `workers.qwen_textual_tool_normalizer`)
and its fail-closed registry. Does not weaken
`workers.protocol_validation.validate_response()` or
`planning.planner.parse_planner_output()` — those keep their own tests.
"""

from __future__ import annotations

import json

import pytest

from code_slayer.planning.planner import (
    STRUCTURED_OUTPUT_FIELDS,
    STRUCTURED_OUTPUT_STRING_FIELDS,
    PlannerFailureCategory,
    PlannerOutcome,
    PlannerRequest,
    ToolCallTransport,
    parse_planner_output,
)
from code_slayer.planning.worker_planner import (
    TOOL_NAME,
    WorkerAdapterPlanner,
    build_default_normalizer_registry,
)
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.protocol import (
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)
from code_slayer.workers.protocol_normalization import (
    NormalizationOutcome,
    ToolProtocolNormalizerRegistry,
)
from code_slayer.workers.qwen_textual_tool_normalizer import (
    _MAX_INPUT_CHARS,
    NORMALIZER_ID,
    NORMALIZER_VERSION,
    QwenTextualToolNormalizer,
)

_ALLOWED = (TOOL_NAME,)


def _normalizer() -> QwenTextualToolNormalizer:
    return QwenTextualToolNormalizer(
        known_parameters=STRUCTURED_OUTPUT_FIELDS,
        string_parameters=STRUCTURED_OUTPUT_STRING_FIELDS,
    )


def _qwen_document(
    *,
    goal: str = "Add a read-only endpoint",
    extra: str = "",
    trailer: str = "",
    prefix: str = "",
    requirements: str | None = None,
    affected_files: str = "[]",
) -> str:
    req_block = ""
    if requirements is not None:
        req_block = f"<parameter=requirements>\n{requirements}\n</parameter>\n"
    return (
        f"{prefix}<function={TOOL_NAME}>\n"
        f"<parameter=goal>\n{goal}\n</parameter>\n"
        f"{req_block}"
        f"<parameter=affected_files>\n{affected_files}\n</parameter>\n"
        f"{extra}"
        f"</function>{trailer}"
    )


# -- registry: explicit, code-owned, fail-closed ------------------------------


def test_registry_resolve_none_when_unconfigured():
    registry = build_default_normalizer_registry()
    assert registry.resolve(None, None) is None
    assert registry.resolve(NORMALIZER_ID, None) is None
    assert registry.resolve(None, NORMALIZER_VERSION) is None


def test_registry_resolve_none_for_unknown_id_or_version():
    registry = build_default_normalizer_registry()
    assert registry.resolve("not_a_real_normalizer", 1) is None
    assert registry.resolve(NORMALIZER_ID, NORMALIZER_VERSION + 1) is None


def test_registry_resolve_exact_implemented_pair():
    registry = build_default_normalizer_registry()
    resolved = registry.resolve(NORMALIZER_ID, NORMALIZER_VERSION)
    assert resolved is not None
    assert resolved.normalizer_id == NORMALIZER_ID
    assert resolved.normalizer_version == NORMALIZER_VERSION


def test_registry_rejects_duplicate_registration():
    n = _normalizer()
    with pytest.raises(ValueError, match="duplicate normalizer"):
        ToolProtocolNormalizerRegistry((n, n))


# -- accepted grammar: real Qwen textual protocol -----------------------------


def test_real_qwen_shape_without_tool_call_trailer_is_accepted():
    """The operator-specified qwen3-coder-ctx16k:30b / Ollama shape."""
    text = _qwen_document()
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.NORMALIZED
    assert result.reason == "qwen_textual_tool_v1_normalized"
    assert result.tool_call is not None
    assert result.tool_call.tool == TOOL_NAME
    assert result.tool_call.params["goal"] == "Add a read-only endpoint"
    assert result.tool_call.params["affected_files"] == []
    assert parse_planner_output(result.tool_call.params) is not None


def test_real_qwen_shape_with_asymmetric_tool_call_close_is_accepted():
    """The 2026-09-14 leak closer: trailing </tool_call>, no opener."""
    text = _qwen_document(trailer="\n</tool_call>\n")
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.NORMALIZED
    assert result.tool_call is not None
    assert result.tool_call.params["goal"] == "Add a read-only endpoint"


def test_json_parameters_parse_strictly():
    text = _qwen_document(requirements='["keep it read-only", "no mutation"]')
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.NORMALIZED
    assert result.tool_call is not None
    assert result.tool_call.params["requirements"] == ["keep it read-only", "no mutation"]


def test_surrounding_whitespace_only_is_accepted():
    text = "\n  " + _qwen_document() + "\n"
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.NORMALIZED


# -- fail closed: no fuzzy repair ---------------------------------------------


def test_surrounding_prose_is_rejected():
    text = _qwen_document(prefix="Here is the plan:\n")
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "non_whitespace_prefix"
    assert result.tool_call is None


def test_suffix_prose_is_rejected():
    text = _qwen_document(trailer="\nThanks!\n")
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "non_whitespace_suffix"


def test_opening_tool_call_tag_is_rejected():
    text = "<tool_call>\n" + _qwen_document(trailer="\n</tool_call>")
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "unexpected_tool_call_open_tag"


def test_multiple_functions_are_rejected():
    text = _qwen_document() + "\n" + _qwen_document(goal="something else")
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "expected_exactly_one_function_invocation"


def test_nested_pseudo_tool_in_parameter_value_is_rejected():
    nested = "<function=emit_engineering_plan>"
    text = _qwen_document(goal=f"do {nested} it")
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason in {
        "expected_exactly_one_function_invocation",
        "nested_pseudo_tool_content",
    }


def test_duplicate_parameter_is_rejected():
    extra = "<parameter=goal>\nother\n</parameter>\n"
    text = _qwen_document(extra=extra)
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "duplicate_parameter"


def test_unknown_parameter_is_rejected():
    extra = "<parameter=not_a_real_field>\ntrue\n</parameter>\n"
    text = _qwen_document(extra=extra)
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "unknown_parameter"


def test_extra_fields_are_rejected_as_unknown_parameters():
    extra = "<parameter=extra_field>\n1\n</parameter>\n"
    text = _qwen_document(extra=extra)
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "unknown_parameter"
    assert result.tool_call is None


def test_unknown_function_name_is_rejected():
    text = "<function=rm_rf>\n<parameter=goal>\nx\n</parameter>\n</function>"
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "unknown_function_name"


def test_malformed_json_parameter_is_rejected():
    text = _qwen_document(affected_files="[not-json")
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "malformed_json_parameter_value"


def test_non_finite_json_constant_is_rejected():
    extra = "<parameter=requirements>\n[NaN]\n</parameter>\n"
    text = _qwen_document(extra=extra)
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "malformed_json_parameter_value"


def test_duplicate_json_object_key_is_rejected():
    extra = (
        '<parameter=planned_changes>\n[{"description": "a", "description": "b"}]\n</parameter>\n'
    )
    text = _qwen_document(extra=extra)
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "malformed_json_parameter_value"


def test_missing_function_close_tag_is_rejected():
    text = f"<function={TOOL_NAME}>\n<parameter=goal>\nx\n</parameter>\n"
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "expected_exactly_one_function_close_tag"


def test_missing_parameter_close_tag_is_rejected():
    text = f"<function={TOOL_NAME}>\n<parameter=goal>\nx\n</function>"
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason in {"missing_parameter_close_tag", "malformed_function_body"}


def test_input_over_bound_is_rejected():
    text = _qwen_document(goal="x" * (_MAX_INPUT_CHARS + 1))
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "input_too_large"


def test_missing_required_goal_still_fails_existing_schema_validation():
    """The decoder must not invent `goal`; `parse_planner_output()` is
    still the schema authority."""
    text = f"<function={TOOL_NAME}>\n<parameter=affected_files>\n[]\n</parameter>\n</function>"
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.NORMALIZED
    assert result.tool_call is not None
    assert parse_planner_output(result.tool_call.params) is None


def test_empty_document_is_rejected():
    result = _normalizer().normalize("", allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED
    assert result.reason == "expected_exactly_one_function_invocation"


def test_plain_prose_is_rejected():
    result = _normalizer().normalize("Here is my plan.", allowed_tools=_ALLOWED)
    assert result.outcome == NormalizationOutcome.REJECTED


# -- planner integration: native precedence, fail closed, existing parser -----


def test_worker_adapter_planner_defaults_to_fail_closed_on_leakage():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(kind=WorkerResponseKind.TEXT, text=_qwen_document()),
        ]
    )
    planner = WorkerAdapterPlanner(adapter, task_id="plan-x")
    response = planner.plan(PlannerRequest(original_request="Add a read-only endpoint"))
    assert response.outcome == PlannerOutcome.MALFORMED
    assert "textual_tool_protocol_leakage" in (response.error or "")
    assert response.tool_call_transport is None


def test_enabled_normalizer_accepts_real_qwen_shape_then_existing_schema():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(kind=WorkerResponseKind.TEXT, text=_qwen_document()),
        ]
    )
    planner = WorkerAdapterPlanner(
        adapter,
        task_id="plan-x",
        normalizer_registry=build_default_normalizer_registry(),
        normalizer_id=NORMALIZER_ID,
        normalizer_version=NORMALIZER_VERSION,
    )
    response = planner.plan(PlannerRequest(original_request="Add a read-only endpoint"))
    assert response.outcome == PlannerOutcome.STRUCTURED
    assert response.output is not None
    assert response.output.goal == "Add a read-only endpoint"
    assert response.tool_call_transport == ToolCallTransport.NORMALIZED
    assert response.normalizer_id == NORMALIZER_ID
    assert response.normalizer_version == NORMALIZER_VERSION
    assert response.normalization_reason == "qwen_textual_tool_v1_normalized"


def test_native_tool_call_takes_precedence_over_enabled_normalizer():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(
                kind=WorkerResponseKind.TOOL_CALL,
                tool_call=WorkerToolCall(tool=TOOL_NAME, params={"goal": "native wins"}),
            ),
        ]
    )
    planner = WorkerAdapterPlanner(
        adapter,
        task_id="plan-x",
        normalizer_registry=build_default_normalizer_registry(),
        normalizer_id=NORMALIZER_ID,
        normalizer_version=NORMALIZER_VERSION,
    )
    response = planner.plan(PlannerRequest(original_request="Do it"))
    assert response.outcome == PlannerOutcome.STRUCTURED
    assert response.output is not None
    assert response.output.goal == "native wins"
    assert response.tool_call_transport == ToolCallTransport.NATIVE
    assert response.normalizer_id is None


def test_enabled_normalizer_still_rejects_non_matching_leakage():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(kind=WorkerResponseKind.TEXT, text="<tool_call>{}</tool_call>"),
        ]
    )
    planner = WorkerAdapterPlanner(
        adapter,
        task_id="plan-x",
        normalizer_registry=build_default_normalizer_registry(),
        normalizer_id=NORMALIZER_ID,
        normalizer_version=NORMALIZER_VERSION,
    )
    response = planner.plan(PlannerRequest(original_request="Do it"))
    assert response.outcome == PlannerOutcome.MALFORMED
    assert response.failure_category == PlannerFailureCategory.NON_TOOL_RESPONSE
    assert "normalization_rejected" in (response.error or "")


def test_normalized_missing_goal_still_fails_existing_planner_parser():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(
                kind=WorkerResponseKind.TEXT,
                text=(
                    f"<function={TOOL_NAME}>\n"
                    "<parameter=affected_files>\n[]\n</parameter>\n"
                    "</function>"
                ),
            ),
        ]
    )
    planner = WorkerAdapterPlanner(
        adapter,
        task_id="plan-x",
        normalizer_registry=build_default_normalizer_registry(),
        normalizer_id=NORMALIZER_ID,
        normalizer_version=NORMALIZER_VERSION,
    )
    response = planner.plan(PlannerRequest(original_request="Do it"))
    assert response.outcome == PlannerOutcome.MALFORMED
    assert response.failure_category == PlannerFailureCategory.SCHEMA_INVALID
    assert response.tool_call_transport == ToolCallTransport.NORMALIZED


def test_incomplete_normalizer_config_is_rejected_at_construction():
    adapter = FakeWorkerAdapter([])
    with pytest.raises(ValueError, match="both be set"):
        WorkerAdapterPlanner(
            adapter,
            task_id="plan-x",
            normalizer_registry=build_default_normalizer_registry(),
            normalizer_id=NORMALIZER_ID,
        )
    with pytest.raises(ValueError, match="require a normalizer_registry"):
        WorkerAdapterPlanner(
            adapter,
            task_id="plan-x",
            normalizer_id=NORMALIZER_ID,
            normalizer_version=NORMALIZER_VERSION,
        )


def test_plain_text_without_leakage_is_never_normalized():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(kind=WorkerResponseKind.TEXT, text="Here is my implementation plan..."),
        ]
    )
    planner = WorkerAdapterPlanner(
        adapter,
        task_id="plan-x",
        normalizer_registry=build_default_normalizer_registry(),
        normalizer_id=NORMALIZER_ID,
        normalizer_version=NORMALIZER_VERSION,
    )
    response = planner.plan(PlannerRequest(original_request="Do it"))
    assert response.outcome == PlannerOutcome.MALFORMED
    assert response.failure_category == PlannerFailureCategory.NON_TOOL_RESPONSE
    assert "text_response" in (response.error or "")
    assert "normalization" not in (response.error or "")


def test_structured_output_fields_match_parser_allowlist():
    """The decoder's known-parameter authority is the parser's own."""
    parsed = parse_planner_output({"goal": "x", "bogus": 1})
    assert parsed is None
    assert "bogus" not in STRUCTURED_OUTPUT_FIELDS
    assert "goal" in STRUCTURED_OUTPUT_STRING_FIELDS


def test_canonical_json_round_trip_of_normalized_params():
    text = _qwen_document(requirements='["a"]')
    result = _normalizer().normalize(text, allowed_tools=_ALLOWED)
    assert result.tool_call is not None
    dumped = json.dumps(dict(result.tool_call.params), sort_keys=True)
    assert json.loads(dumped)["goal"] == "Add a read-only endpoint"


def test_planner_output_provenance_distinguishes_native_from_normalized(db_conn, tmp_path):
    from code_slayer.planning.provenance import store_planner_output
    from code_slayer.store.content_store import ContentStore

    store = ContentStore(db_conn, tmp_path / "blobs")
    native_response = WorkerAdapterPlanner(
        FakeWorkerAdapter(
            [
                WorkerResponse(
                    kind=WorkerResponseKind.TOOL_CALL,
                    tool_call=WorkerToolCall(tool=TOOL_NAME, params={"goal": "native"}),
                ),
            ]
        ),
        task_id="n",
    ).plan(PlannerRequest(original_request="Do it"))
    leaked_response = WorkerAdapterPlanner(
        FakeWorkerAdapter(
            [
                WorkerResponse(
                    kind=WorkerResponseKind.TEXT, text=_qwen_document(goal="normalized")
                ),
            ]
        ),
        task_id="z",
        normalizer_registry=build_default_normalizer_registry(),
        normalizer_id=NORMALIZER_ID,
        normalizer_version=NORMALIZER_VERSION,
    ).plan(PlannerRequest(original_request="Do it"))
    native_blob = store_planner_output(store, native_response)
    leaked_blob = store_planner_output(store, leaked_response)
    native_doc = json.loads(store.read(native_blob.content_hash))
    leaked_doc = json.loads(store.read(leaked_blob.content_hash))
    assert native_doc["tool_call_transport"] == ToolCallTransport.NATIVE.value
    assert native_doc["normalizer_id"] is None
    assert leaked_doc["tool_call_transport"] == ToolCallTransport.NORMALIZED.value
    assert leaked_doc["normalizer_id"] == NORMALIZER_ID
    assert leaked_doc["normalizer_version"] == NORMALIZER_VERSION
    assert native_blob.content_hash != leaked_blob.content_hash
