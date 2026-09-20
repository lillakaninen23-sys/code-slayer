"""`coding.tool_loop._build_tool_request()`: strict, structural
translation of a model-claimed tool call into `tools.models.ToolRequest`
-- never trusted beyond shape (every real authorization decision still
happens inside `tools.executor.ToolExecutor`/`policy.engine.PolicyEngine`
unmodified), but malformed shapes must be rejected here, before any
attempt to execute, with a stable `ToolLoopContractError`.
"""

from __future__ import annotations

import json

import pytest

from code_slayer.audit.canonical import canonical_json
from code_slayer.coding import qualification, tool_loop
from code_slayer.coding.tool_loop import (
    CODER_TOOLS,
    MAX_AUTHORIZED_READ_CHARS,
    ToolLoopContractError,
    _build_tool_request,
    format_authorized_read_result,
)
from code_slayer.tools.models import ToolRequest

_HASH64 = "a" * 64


def test_coder_tools_is_the_expected_closed_set():
    assert CODER_TOOLS == ("read_file", "create_file", "write_file", "apply_patch")
    assert "run_command" not in CODER_TOOLS
    assert "checkpoint_create" not in CODER_TOOLS


def test_read_file_request_builds_cleanly():
    request = _build_tool_request("read_file", {"path": "a.py"})
    assert request == ToolRequest(tool="read_file", path="a.py")


def test_read_file_rejects_extra_params():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("read_file", {"path": "a.py", "content": "x"})


def test_create_file_request_builds_cleanly():
    request = _build_tool_request("create_file", {"path": "a.py", "content": "print(1)\n"})
    assert request.tool == "create_file"
    assert request.path == "a.py"
    assert request.content == b"print(1)\n"


def test_create_file_rejects_non_string_content():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("create_file", {"path": "a.py", "content": 123})


def test_write_file_requires_expected_hash():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("write_file", {"path": "a.py", "content": "x"})


def test_write_file_rejects_malformed_expected_hash():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request(
            "write_file", {"path": "a.py", "content": "x", "expected_hash": "short"},
        )


def test_write_file_request_builds_cleanly():
    request = _build_tool_request(
        "write_file", {"path": "a.py", "content": "x", "expected_hash": _HASH64},
    )
    assert request.tool == "write_file"
    assert request.expected_hash == _HASH64
    assert request.content == b"x"


def test_apply_patch_requires_at_least_one_hunk():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("apply_patch", {"path": "a.py", "expected_hash": _HASH64, "hunks": []})


def test_apply_patch_rejects_malformed_hunk_shape():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request(
            "apply_patch",
            {"path": "a.py", "expected_hash": _HASH64, "hunks": [{"offset": 0, "before": "x"}]},
        )


def test_apply_patch_request_builds_cleanly():
    request = _build_tool_request(
        "apply_patch",
        {
            "path": "a.py", "expected_hash": _HASH64,
            "hunks": [{"offset": 0, "before": "old", "after": "new"}],
        },
    )
    assert request.tool == "apply_patch"
    assert len(request.hunks) == 1
    assert request.hunks[0].before == b"old"
    assert request.hunks[0].after == b"new"


def test_unoffered_tool_is_rejected():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("run_command", {"path": "a.py"})


def test_missing_path_is_rejected():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("read_file", {})


def test_non_mapping_params_is_rejected():
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("read_file", "a.py")
    with pytest.raises(ToolLoopContractError):
        _build_tool_request("read_file", ["a.py"])


def test_authorized_read_result_contains_content_and_expected_hash():
    payload = b"def add(a, b):\n    return a - b\n"
    digest = "e" * 64
    raw = format_authorized_read_result(payload, digest)
    data = json.loads(raw)
    assert data == {
        "content": payload.decode(),
        "expected_hash": digest,
        "truncated": False,
    }
    assert raw == canonical_json(data)


def test_authorized_read_result_bounds_oversized_content_without_dropping_hash():
    payload = ("x" * (MAX_AUTHORIZED_READ_CHARS + 50)).encode()
    digest = "f" * 64
    data = json.loads(format_authorized_read_result(payload, digest))
    assert data["truncated"] is True
    assert data["content"] == "x" * MAX_AUTHORIZED_READ_CHARS
    assert data["expected_hash"] == digest
    assert len(data["content"]) == MAX_AUTHORIZED_READ_CHARS


def test_qualification_and_production_share_authorized_read_helper():
    assert qualification.format_authorized_read_result is tool_loop.format_authorized_read_result
