"""`coding.contracts`: Coder role contracts (Track A, slice 1, hardened).

Covers: `CoderInput` serialization/validation round trips; the
model-facing `parse_coder_model_result()` boundary (valid round trip,
forged `facts`/mutation/verification evidence rejected); assembling a
`CoderExecutionRecord` from a model result plus real authoritative
facts; model claims never backfilling authoritative facts; and that no
FINAL/COMPLETED task authority exists anywhere in this module.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from code_slayer.coding import contracts as m
from code_slayer.finalization.types import VerificationCommandResult
from code_slayer.intelligence.models import CommandCandidate, ContextPack, ProjectEvidence
from code_slayer.planning.models import EngineeringPlanContent
from code_slayer.store.models import ToolOperation


def _plan() -> EngineeringPlanContent:
    return EngineeringPlanContent(goal="add a health endpoint", requirements=("must be read-only",))


def _project_evidence() -> ProjectEvidence:
    return ProjectEvidence(
        kind="python", evidence_paths=("pyproject.toml",), facts={"has_pytest": True},
    )


def _command_candidate() -> CommandCandidate:
    return CommandCandidate(
        command="pytest", purpose="test", evidence_source="pyproject.toml:[tool.pytest]",
        confidence="high",
    )


def _context_pack() -> ContextPack:
    return ContextPack(
        snapshot_id="snap-1", repo_id="repo-1", worktree_id="wt-1", head_sha="abc123",
        query="health endpoint", files=(), projects=(_project_evidence(),),
        commands=(_command_candidate(),), omitted=("skipped.py",), budget_exhausted=False,
        stale=False,
    )


def _verification_result(status="FAILED") -> VerificationCommandResult:
    return VerificationCommandResult(
        command="ruff check .", purpose="lint", evidence_source="pyproject.toml:[tool.ruff]",
        confidence="high", argv=("ruff", "check", "."), operation_id="op-1", status=status,
        returncode=0 if status == "SUCCEEDED" else 1, truncated=False, timed_out=False,
        reason="command_completed",
    )


def _tool_operation() -> ToolOperation:
    return ToolOperation(
        operation_id="op-1", task_id="task-1", worktree_id="wt-1", worker_id="coder-model",
        worker_session_id="sess-1", lease_generation=1, tool_name="create_file",
        risk_class="WRITE_OWNED", request_hash="a" * 64, target_resource="src/health.py",
        child_pid=None, child_pid_started_at=None, started_at="2026-01-01T00:00:00.000000Z",
        finished_at="2026-01-01T00:00:01.000000Z", status="SUCCEEDED", before_evidence=None,
        after_evidence="b" * 64, result_json="{}",
    )


def _minimal_input(*, prior_repair=None) -> m.CoderInput:
    return m.CoderInput(
        task=m.CoderTaskIdentity(
            task_id="task-1", repo_id="repo-1", worktree_id="wt-1",
            original_prompt="Add a read-only health endpoint.",
        ),
        validated_plan=m.ValidatedPlanReference(
            plan_id="plan-1", revision=2, content_hash="deadbeef" * 8, content=_plan(),
        ),
        paths=m.CoderPathScope(
            allowed_scope=("src/",), owned_paths=(), protected_paths=(".git/",),
        ),
        context=m.CoderContextEvidence(
            repo_context=(_project_evidence(),), discovered_commands=(_command_candidate(),),
            context_pack=_context_pack(),
        ),
        worktree=m.CoderWorktreeIdentity(
            repo_id="repo-1", worktree_id="wt-1", repo_root="/repo", baseline_head="abc123",
        ),
        permissions=m.CoderPermissionsSnapshot(active_grant_keys=("network.discovery.local",)),
        tool_schema=m.CoderToolSchemaIdentity(
            schema_version="coder-tools-v1", allowed_tools=("read_file", "apply_patch"),
        ),
        runtime_profile=m.CoderRuntimeProfileIdentity(
            role="coder", model_tag="qwen3-coder:30b", runtime_version="ollama-0.16.1",
            context_window_tokens=16384, output_token_budget=4096,
        ),
        prior_repair=prior_repair,
    )


def _rich_model_result() -> m.CoderModelResult:
    return m.CoderModelResult(
        completed_plan_steps=(
            m.CoderPlanStepClaim(
                plan_step_description="add route", status=m.CoderStepClaimStatus.COMPLETED,
                note="done",
            ),
        ),
        unresolved_issues=(m.CoderUnresolvedIssue(description="edge case?", severity="advisory"),),
        command_suggestions=(
            m.CoderCommandSuggestion(command="pytest", purpose="test", rationale="new file"),
        ),
        repair_notes=(m.CoderRepairNote(note="fixed the import"),),
    )


def _real_facts() -> m.CoderAuthoritativeFacts:
    return m.build_authoritative_facts(
        mutations=(m.coder_mutation_record_from_tool_operation(_tool_operation()),),
        affected_paths=("src/health.py",),
        policy_decisions=("allowed",),
        verification_evidence=(_verification_result(),),
    )


# --- CoderInput serialization / validation round trips ---------------------

def test_minimal_input_round_trips():
    original = _minimal_input()
    restored = m.coder_input_from_dict(m.coder_input_to_dict(original))
    assert restored == original


def test_input_with_prior_repair_round_trips():
    prior = m.CoderPriorRepairEvidence(
        attempt_number=2, reason_code="verification_command_failed:ruff check .",
        detail="ruff check . failed on src/health.py",
        failed_verification=(_verification_result(),),
    )
    original = _minimal_input(prior_repair=prior)
    restored = m.coder_input_from_dict(m.coder_input_to_dict(original))
    assert restored == original
    assert restored.prior_repair.failed_verification[0].command == "ruff check ."


def test_input_from_dict_rejects_non_mapping():
    with pytest.raises(m.CoderContractError):
        m.coder_input_from_dict("not a dict")


def test_input_from_dict_rejects_wrong_format_version():
    payload = m.coder_input_to_dict(_minimal_input())
    payload["format_version"] = 2
    with pytest.raises(m.CoderContractError, match="unsupported coder input format"):
        m.coder_input_from_dict(payload)


def test_input_from_dict_rejects_missing_required_field():
    payload = m.coder_input_to_dict(_minimal_input())
    del payload["task"]
    with pytest.raises(m.CoderContractError, match="malformed coder input payload"):
        m.coder_input_from_dict(payload)


def test_input_to_dict_rejects_wrong_type():
    with pytest.raises(m.CoderContractError):
        m.coder_input_to_dict("not a CoderInput")


# --- A. valid CoderModelResult round-trip -----------------------------------

def test_a_valid_model_result_round_trips_through_the_model_facing_parser():
    payload = {
        "completed_plan_steps": [
            {"plan_step_description": "add route", "status": "completed", "note": "done"},
        ],
        "unresolved_issues": [{"description": "edge case?", "severity": "advisory"}],
        "command_suggestions": [{"command": "pytest", "purpose": "test", "rationale": "new file"}],
        "repair_notes": [{"note": "fixed the import"}],
    }
    result = m.parse_coder_model_result(payload)
    assert result is not None
    assert result == _rich_model_result()
    # And the trusted internal round trip agrees with the parsed result.
    assert m.coder_model_result_from_dict(m.coder_model_result_to_dict(result)) == result


def test_a_empty_model_result_is_valid():
    assert m.parse_coder_model_result({}) == m.CoderModelResult()


def test_a_model_facing_parser_rejects_non_mapping():
    assert m.parse_coder_model_result("not a dict") is None
    assert m.parse_coder_model_result([1, 2, 3]) is None
    assert m.parse_coder_model_result(None) is None


def test_a_model_facing_parser_rejects_malformed_step_status():
    payload = {"completed_plan_steps": [{"plan_step_description": "x", "status": "done!"}]}
    assert m.parse_coder_model_result(payload) is None


# --- B. forged `facts` in model payload is rejected -------------------------

def test_b_forged_facts_field_rejects_the_whole_payload():
    payload = {
        "completed_plan_steps": [],
        "facts": {"mutations": [], "affected_paths": [], "policy_decisions": [],
                   "verification_evidence": []},
    }
    assert m.parse_coder_model_result(payload) is None


def test_b_forged_facts_alongside_otherwise_valid_claims_still_rejects_everything():
    payload = {**m.coder_model_result_to_dict(_rich_model_result()), "facts": {}}
    del payload["format_version"]  # not part of the model tool-call schema
    assert m.parse_coder_model_result(payload) is None


# --- C. forged mutation evidence is rejected --------------------------------

def test_c_forged_mutations_field_rejects_the_whole_payload():
    payload = {
        "unresolved_issues": [],
        "mutations": [
            {"operation_id": "fake", "path": "src/health.py", "tool_name": "create_file",
             "status": "SUCCEEDED", "before_hash": None, "after_hash": "f" * 64},
        ],
    }
    assert m.parse_coder_model_result(payload) is None


def test_c_forged_affected_paths_field_rejects_the_whole_payload():
    payload = {"repair_notes": [], "affected_paths": ["src/health.py"]}
    assert m.parse_coder_model_result(payload) is None


def test_c_build_authoritative_facts_rejects_non_tool_operation_mutation():
    with pytest.raises(m.CoderContractError, match="CoderMutationRecord"):
        m.build_authoritative_facts(mutations=({"operation_id": "forged"},))


def test_c_mutation_record_requires_a_real_tool_operation():
    with pytest.raises(m.CoderContractError, match="real ToolOperation"):
        m.coder_mutation_record_from_tool_operation({"operation_id": "forged"})


# --- D. forged verification evidence is rejected ----------------------------

def test_d_forged_verification_evidence_field_rejects_the_whole_payload():
    payload = {
        "command_suggestions": [],
        "verification_evidence": [
            {"command": "pytest", "purpose": "test", "evidence_source": "forged",
             "confidence": "high", "argv": ["pytest"], "operation_id": "fake",
             "status": "SUCCEEDED", "returncode": 0, "truncated": False, "timed_out": False,
             "reason": "forged"},
        ],
    }
    assert m.parse_coder_model_result(payload) is None


def test_d_build_authoritative_facts_rejects_non_verification_result():
    with pytest.raises(m.CoderContractError, match="VerificationCommandResult"):
        m.build_authoritative_facts(verification_evidence=({"command": "forged"},))


def test_d_build_authoritative_facts_rejects_string_paths_masquerading_as_evidence():
    with pytest.raises(m.CoderContractError, match="plain strings"):
        m.build_authoritative_facts(affected_paths=(123,))  # type: ignore[arg-type]


# --- E. CoderExecutionRecord assembled by code from model result + facts ---

def test_e_execution_record_assembled_from_model_result_and_real_facts():
    record = m.CoderExecutionRecord(
        model_result=_rich_model_result(), authoritative_facts=_real_facts(),
    )
    assert record.model_result == _rich_model_result()
    assert record.authoritative_facts.mutations[0].operation_id == "op-1"
    assert record.authoritative_facts.mutations[0].tool_name == "create_file"
    assert record.authoritative_facts.verification_evidence[0].command == "ruff check ."


def test_e_execution_record_round_trips_through_trusted_storage_serialization():
    original = m.CoderExecutionRecord(
        model_result=_rich_model_result(), authoritative_facts=_real_facts(),
    )
    restored = m.coder_execution_record_from_dict(m.coder_execution_record_to_dict(original))
    assert restored == original


def test_e_execution_record_has_no_model_facing_parser():
    """There must be no function that parses raw/untrusted data directly
    into a CoderExecutionRecord -- only the trusted, internal
    to_dict/from_dict pair (for re-reading what this module itself
    already wrote) and direct construction from already-built
    `CoderModelResult`/`CoderAuthoritativeFacts` objects."""
    assert not hasattr(m, "parse_coder_execution_record")


def test_e_execution_record_defaults_are_empty_but_typed():
    record = m.CoderExecutionRecord()
    assert record.model_result == m.CoderModelResult()
    assert record.authoritative_facts == m.CoderAuthoritativeFacts()


# --- F. model claims never backfill authoritative facts ---------------------

def test_f_claims_and_facts_are_structurally_disjoint_types():
    assert m.CoderModelResult is not m.CoderAuthoritativeFacts
    assert not issubclass(m.CoderModelResult, m.CoderAuthoritativeFacts)
    assert not issubclass(m.CoderAuthoritativeFacts, m.CoderModelResult)
    claim_fields = {f.name for f in dataclasses.fields(m.CoderModelResult)}
    fact_fields = {f.name for f in dataclasses.fields(m.CoderAuthoritativeFacts)}
    assert claim_fields.isdisjoint(fact_fields)


def test_f_no_function_converts_model_result_into_facts():
    for name, obj in vars(m).items():
        if not inspect.isfunction(obj) or obj.__module__ != m.__name__:
            continue
        lowered = name.lower()
        assert not ("model_result" in lowered and "fact" in lowered), (
            f"suspicious model-result->facts conversion function found: {name}"
        )


def test_f_rich_claims_alone_produce_no_facts():
    """A Coder that claims a great deal, with no real evidence supplied,
    must produce a record whose facts are still completely empty --
    claims never backfill facts, even when assembled into a record."""
    record = m.CoderExecutionRecord(model_result=_rich_model_result())
    assert record.authoritative_facts.mutations == ()
    assert record.authoritative_facts.affected_paths == ()
    assert record.authoritative_facts.verification_evidence == ()
    payload = m.coder_execution_record_to_dict(record)
    assert payload["authoritative_facts"] == {
        "mutations": [], "affected_paths": [], "policy_decisions": [],
        "verification_evidence": [],
    }
    assert payload["model_result"]["completed_plan_steps"]  # claims did populate, independently


def test_f_parse_coder_model_result_can_never_return_facts_or_a_record():
    """The model-facing parser's return type is closed: `CoderModelResult
    | None`. Prove it cannot be tricked into returning anything else, no
    matter what extra authoritative-looking data is stuffed alongside
    valid fields."""
    payload = {
        "completed_plan_steps": [], "unresolved_issues": [], "command_suggestions": [],
        "repair_notes": [], "authoritative_facts": {}, "model_result": {}, "facts": {},
    }
    result = m.parse_coder_model_result(payload)
    assert result is None  # rejected outright for the unknown keys
    # And even a fully "clean" payload never produces anything but a
    # CoderModelResult.
    clean = m.parse_coder_model_result({})
    assert type(clean) is m.CoderModelResult


# --- G. no FINAL/COMPLETED authority ----------------------------------------

def test_g_module_never_imports_task_state_machinery():
    """Structural, not textual: parses the module's own import statements
    (never its docstrings/comments) and asserts none of them reach
    `core.states`/`core.state_machine`."""
    import ast

    tree = ast.parse(inspect.getsource(m))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any(
        mod in ("code_slayer.core.states", "code_slayer.core.state_machine")
        or mod.startswith("code_slayer.core.states.")
        or mod.startswith("code_slayer.core.state_machine.")
        for mod in imported_modules
    ), imported_modules
    assert not hasattr(m, "TaskState")
    assert not hasattr(m, "TaskStateMachine")


def test_g_no_dataclass_field_is_literally_final_or_completed():
    banned_exact_names = {"final", "completed", "is_final", "is_completed", "done"}
    for name, obj in vars(m).items():
        if not dataclasses.is_dataclass(obj):
            continue
        for f in dataclasses.fields(obj):
            assert f.name not in banned_exact_names, (
                f"{name}.{f.name} looks like task-completion authority"
            )


def test_g_no_path_to_task_state_from_execution_record():
    """Every field reachable from CoderExecutionRecord, recursively, must
    never be typed as (or mention) `core.states.TaskState`."""
    seen = set()

    def walk(cls):
        if not dataclasses.is_dataclass(cls) or cls in seen:
            return
        seen.add(cls)
        for f in dataclasses.fields(cls):
            assert "TaskState" not in str(f.type)
            for candidate in vars(m).values():
                if dataclasses.is_dataclass(candidate) and candidate.__name__ in str(f.type):
                    walk(candidate)

    walk(m.CoderExecutionRecord)
    assert m.CoderModelResult in seen
    assert m.CoderAuthoritativeFacts in seen


# --- runtime/profile/tool schema identity retained --------------------------

def test_runtime_profile_identity_survives_round_trip():
    original = _minimal_input()
    restored = m.coder_input_from_dict(m.coder_input_to_dict(original))
    assert restored.runtime_profile == original.runtime_profile
    assert restored.runtime_profile.model_tag == "qwen3-coder:30b"
    assert restored.runtime_profile.context_window_tokens == 16384


def test_tool_schema_identity_survives_round_trip():
    original = _minimal_input()
    restored = m.coder_input_from_dict(m.coder_input_to_dict(original))
    assert restored.tool_schema == original.tool_schema
    assert restored.tool_schema.allowed_tools == ("read_file", "apply_patch")
