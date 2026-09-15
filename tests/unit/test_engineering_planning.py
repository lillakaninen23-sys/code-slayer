"""Durable engineering planning (Phase 8.2).

Covers: structured-plan persistence across restart; evidence validation
(existing-file claims never become fact merely because the planner
named them, proposed new files stay distinct, discovered commands must
be authoritative, unsupported claims are advisory); malformed structured
output rejected safely; Question Gate integration (blocking ambiguity ->
NEEDS_INPUT, durable resolution -> resume -> READY); stale-plan
detection bound to Phase 8.1a's own content-safe identity, never
duplicated; the read-only/no-mutation/no-execution/no-trust guarantees;
and reuse of the existing worker transport/protocol-validation stack for
a real planner adapter.
"""

from __future__ import annotations

import pytest

from code_slayer.intelligence.service import RepositoryIntelligenceService
from code_slayer.planning.evidence import validate_plan_against_intelligence
from code_slayer.planning.fake_planner import FakePlanner, FakePlannerError
from code_slayer.planning.models import PlanState
from code_slayer.planning.planner import (
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    parse_planner_output,
)
from code_slayer.planning.service import EngineeringPlanningService
from code_slayer.planning.worker_planner import TOOL_NAME, WorkerAdapterPlanner
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.prompt_analysis import EvidenceSource
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from code_slayer.workers.question_gate import ResolutionKind
from tests.repo_helpers import git

REQUEST = "Add a read-only endpoint reporting repository intelligence snapshot age."


def structured(goal="Add the endpoint", **overrides):
    data = {
        "goal": goal,
        "requirements": [], "assumptions": [], "affected_files": [],
        "planned_changes": [], "dependencies": [], "risks": [], "verification_steps": [],
        "discovered_commands": [], "authority_requirements": [], "evidence_claims": [],
        "ambiguities": [],
    }
    data.update(overrides)
    return parse_planner_output(data)


def structured_response(**overrides) -> PlannerResponse:
    output = structured(**overrides)
    assert output is not None, "test fixture produced unparsable structured output"
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


@pytest.fixture
def state_root(tmp_path):
    root = tmp_path / "_codeslayer_state"
    root.mkdir()
    return root


@pytest.fixture
def service(git_repo_with_commit, state_root):
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    yield svc
    svc.close()


# --- 1. structured plan persists across restart -----------------------------

def test_plan_persists_across_restart(git_repo_with_commit, state_root):
    planner = FakePlanner([structured_response(goal="Persist me")])
    first = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        created = first.create(original_request=REQUEST, planner=planner)
        assert created.state == PlanState.READY.value
    finally:
        first.close()

    restarted = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        got = restarted.get(created.plan_id)
        assert got.state == PlanState.READY.value
        assert got.content.goal == "Persist me"
        assert got.revision == 1 and got.predecessor_plan_id is None
    finally:
        restarted.close()


# --- 2/3. existing-file claims require authoritative evidence --------------

def test_affected_existing_file_requires_authoritative_evidence(service, git_repo_with_commit):
    planner = FakePlanner([structured_response(affected_files=[
        {"path": "README.md", "action": "modify", "reason": "document it"},
    ])])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.READY.value
    affected = record.content.affected_files[0]
    assert affected.exists_in_repository is True
    assert affected.evidence and affected.evidence[0].kind == "file_exists"


def test_nonexistent_existing_file_claim_cannot_become_fact(service, git_repo_with_commit):
    planner = FakePlanner([structured_response(affected_files=[
        {"path": "does_not_exist.py", "action": "modify", "reason": "bogus claim"},
    ])])
    record = service.create(original_request=REQUEST, planner=planner)
    # Never silently promoted to READY -- the false claim blocks this
    # attempt and requires a fresh replan(), never an auto-correction.
    assert record.state == PlanState.DRAFT.value
    assert record.reason == "evidence_validation_failed"
    affected = record.content.affected_files[0]
    assert affected.exists_in_repository is False
    assert any("does_not_exist.py" in issue for issue in record.content.validation_issues)


# --- 4. proposed NEW file is represented distinctly from existing fact -----

def test_proposed_new_file_is_distinct_from_existing_fact(service, git_repo_with_commit):
    planner = FakePlanner([structured_response(affected_files=[
        {"path": "README.md", "action": "modify", "reason": "existing"},
        {"path": "brand_new_module.py", "action": "create", "reason": "new"},
    ])])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.READY.value
    by_path = {f.path: f for f in record.content.affected_files}
    assert by_path["README.md"].exists_in_repository is True
    assert by_path["brand_new_module.py"].exists_in_repository is False
    assert by_path["brand_new_module.py"].action == "create"
    assert by_path["brand_new_module.py"].evidence[0].kind == "file_absent"


def test_create_action_against_an_already_existing_path_is_a_defect(service):
    planner = FakePlanner([structured_response(affected_files=[
        {"path": "README.md", "action": "create", "reason": "wrong"},
    ])])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.DRAFT.value
    assert record.reason == "evidence_validation_failed"


# --- 5. discovered commands must be authoritative ---------------------------

def test_discovered_command_must_come_from_repository_intelligence(service, git_repo_with_commit):
    (git_repo_with_commit / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    planner = FakePlanner([structured_response(discovered_commands=[
        {"command": "pytest", "purpose": "test", "evidence_source": "fabricated"},
        {"command": "rm -rf /", "purpose": "test", "evidence_source": "fabricated"},
    ])])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.READY.value
    commands = {c.command for c in record.content.discovered_commands}
    # The fabricated destructive command never survives -- only a command
    # matching a real, authoritative CommandCandidate does, and even then
    # with the candidate's OWN evidence_source, never the planner's claim.
    assert "rm -rf /" not in commands
    if "pytest" in commands:
        surviving = next(c for c in record.content.discovered_commands if c.command == "pytest")
        assert surviving.evidence_source != "fabricated"
    assert any("not_found_in_repository" in issue for issue in record.content.validation_issues)


# --- 6. malformed planner output rejected safely ----------------------------

def test_malformed_planner_output_rejected_safely(service):
    for bad in (None, {}, {"goal": ""}, {"goal": "x", "unknown_field": 1}, "just text", 42):
        planner = FakePlanner([PlannerResponse(PlannerOutcome.MALFORMED, raw=str(bad))])
        record = service.create(original_request=REQUEST, planner=planner)
        assert record.state == PlanState.DRAFT.value
        assert record.reason == "malformed_planner_output"


def test_planner_returning_garbage_is_never_trusted(service):
    planner = FakePlanner(["not a PlannerResponse at all"])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.DRAFT.value
    assert record.reason == "malformed_planner_output"


def test_parse_planner_output_rejects_unknown_and_wrong_shaped_fields():
    assert parse_planner_output({"goal": "x", "affected_files": [{"path": "a"}]}) is None
    assert parse_planner_output({"goal": "x", "affected_files": "not a list"}) is None
    assert parse_planner_output({"goal": 5}) is None
    assert parse_planner_output(["goal", "x"]) is None
    assert parse_planner_output({}) is None


# --- 7. unsupported repo claim rejected or remains advisory -----------------

def test_unsupported_evidence_claim_is_advisory_not_blocking(service, git_repo_with_commit):
    planner = FakePlanner([structured_response(evidence_claims=[
        {"kind": "symbol_exists", "key": "totally_fake_symbol"},
        {"kind": "file_exists", "key": "README.md"},
    ])])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.READY.value  # advisory only, never blocking
    kinds = {(e.kind, e.key) for e in record.content.evidence_refs}
    assert ("file_exists", "README.md") in kinds
    assert ("symbol_exists", "totally_fake_symbol") not in kinds
    assert any("totally_fake_symbol" in issue for issue in record.content.validation_issues)


# --- 8. unresolved blocking question -> NEEDS_INPUT -------------------------

def test_unresolved_blocking_ambiguity_yields_needs_input(service):
    planner = FakePlanner([structured_response(ambiguities=[{
        "id": "scope", "question": "Which service should this call?",
        "rationale": "no evidence names one", "risk_class": "EXTERNAL_SIDE_EFFECT",
    }])])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.NEEDS_INPUT.value
    assert record.reason == "blocked_on_questions"
    assert len(record.questions) == 1
    assert record.questions[0]["ambiguity_id"] == "scope"
    assert record.questions[0]["resolved"] is False


# --- 9. durable user resolution -> planning can resume ----------------------

def test_durable_resolution_lets_planning_resume_to_ready(service):
    planner = FakePlanner([structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.state == PlanState.NEEDS_INPUT.value

    service.record_user_resolution(
        record.plan_id, "scope", "JSON", resolution_kind=ResolutionKind.FACT,
        source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    resumed = service.resume(record.plan_id)
    assert resumed.state == PlanState.READY.value
    assert resumed.questions[0]["resolved"] is True
    assert resumed.questions[0]["answer_recorded"] is True


def test_resume_never_reinvokes_the_planner(service):
    planner = FakePlanner([structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])])
    record = service.create(original_request=REQUEST, planner=planner)
    service.record_user_resolution(
        record.plan_id, "scope", "JSON", resolution_kind=ResolutionKind.FACT,
    )
    service.resume(record.plan_id)
    service.resume(record.plan_id)  # a second resume must not exhaust the fake's queue
    assert len(planner.calls) == 1


# --- 10/11. READY plan becomes stale after HEAD/dirty content change -------

def test_ready_plan_becomes_stale_after_head_change(service, git_repo_with_commit):
    planner = FakePlanner([structured_response()])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.effective_state == PlanState.READY.value

    (git_repo_with_commit / "new.txt").write_text("x\n")
    git(git_repo_with_commit, "add", "new.txt")
    git(git_repo_with_commit, "commit", "-q", "-m", "second commit")

    refreshed = service.get(record.plan_id)
    assert refreshed.state == PlanState.READY.value  # durable state itself unchanged
    assert refreshed.effective_state == "STALE"      # but reported as stale


def test_ready_plan_becomes_stale_after_dirty_content_change(service, git_repo_with_commit):
    planner = FakePlanner([structured_response()])
    record = service.create(original_request=REQUEST, planner=planner)
    assert record.effective_state == PlanState.READY.value

    (git_repo_with_commit / "README.md").write_text("changed content, same HEAD\n")

    refreshed = service.get(record.plan_id)
    assert refreshed.effective_state == "STALE"


# --- 12. stale historical plan remains readable/auditable -------------------

def test_stale_plan_remains_readable_after_repository_changes(service, git_repo_with_commit):
    planner = FakePlanner([structured_response(goal="Historical goal")])
    record = service.create(original_request=REQUEST, planner=planner)
    (git_repo_with_commit / "README.md").write_text("changed\n")

    got = service.get(record.plan_id)
    assert got.effective_state == "STALE"
    assert got.content is not None
    assert got.content.goal == "Historical goal"
    listed = service.list()
    assert any(p.plan_id == record.plan_id for p in listed)


# --- 13/14/15. no mutation, no execution, repeated reads are inert ---------

def test_repeated_reads_do_not_mutate_repository_or_state(service, git_repo_with_commit):
    planner = FakePlanner([structured_response()])
    record = service.create(original_request=REQUEST, planner=planner)
    before = git(git_repo_with_commit, "status", "--porcelain")
    for _ in range(3):
        service.get(record.plan_id)
        service.status(record.plan_id)
        service.list()
    after = git(git_repo_with_commit, "status", "--porcelain")
    assert before == after


def test_planning_performs_no_repository_mutation(service, git_repo_with_commit):
    planner = FakePlanner([structured_response(affected_files=[
        {"path": "README.md", "action": "modify", "reason": "doc"},
    ])])
    before = git(git_repo_with_commit, "status", "--porcelain")
    original_bytes = (git_repo_with_commit / "README.md").read_bytes()
    service.create(original_request=REQUEST, planner=planner)
    assert git(git_repo_with_commit, "status", "--porcelain") == before
    assert (git_repo_with_commit / "README.md").read_bytes() == original_bytes


def test_planning_never_executes_discovered_commands(service, git_repo_with_commit):
    (git_repo_with_commit / "Makefile").write_text("test:\n\ttouch executed.marker\n")
    planner = FakePlanner([structured_response(discovered_commands=[
        {"command": "make test", "purpose": "test", "evidence_source": "fabricated"},
    ])])
    service.create(original_request=REQUEST, planner=planner)
    assert not (git_repo_with_commit / "executed.marker").exists()


# --- 16. no new mutation capability/trust granted ---------------------------

def test_planning_grants_no_trust_and_imports_no_mutation_authority():
    """Structural, not textual: parses the real import statements (never
    a docstring mentioning these modules by name for explanatory
    purposes, which this very module's own docstring does)."""
    import ast

    import code_slayer.planning.service as planning_service_module

    tree = ast.parse(open(planning_service_module.__file__, encoding="utf-8").read())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    for forbidden in (
        "code_slayer.tools.executor", "code_slayer.policy.engine",
        "code_slayer.lease.manager", "code_slayer.repo.checkpoint",
        "code_slayer.workers.trust", "code_slayer.workers.promotion",
    ):
        assert forbidden not in imported_modules, f"planning.service must never import {forbidden}"


# --- replan() ----------------------------------------------------------------

def test_replan_creates_new_revision_and_supersedes_old(service):
    first_planner = FakePlanner([structured_response(affected_files=[
        {"path": "does_not_exist.py", "action": "modify", "reason": "bogus"},
    ])])
    first = service.create(original_request=REQUEST, planner=first_planner)
    assert first.state == PlanState.DRAFT.value

    second_planner = FakePlanner([structured_response(goal="Corrected plan")])
    second = service.replan(first.plan_id, planner=second_planner)
    assert second.revision == 2
    assert second.predecessor_plan_id == first.plan_id
    assert second.state == PlanState.READY.value

    old = service.get(first.plan_id)
    assert old.state == PlanState.SUPERSEDED.value
    assert old.content.affected_files[0].path == "does_not_exist.py"  # never rewritten


def test_replan_of_already_superseded_plan_is_rejected(service):
    planner = FakePlanner([structured_response(), structured_response()])
    first = service.create(original_request=REQUEST, planner=planner)
    service.replan(first.plan_id, planner=FakePlanner([structured_response()]))
    with pytest.raises(ValueError):
        service.replan(first.plan_id, planner=FakePlanner([structured_response()]))


# --- evidence.py direct unit coverage --------------------------------------

def test_evidence_validation_is_pure_and_deterministic(git_repo_with_commit):
    intel = RepositoryIntelligenceService(git_repo_with_commit)
    try:
        snapshot = intel.inspect()
    finally:
        intel.close()
    output = structured(affected_files=[{"path": "README.md", "action": "inspect", "reason": "r"}])
    first = validate_plan_against_intelligence(output, snapshot)
    second = validate_plan_against_intelligence(output, snapshot)
    assert first.content == second.content
    assert first.blocking == second.blocking is False


# --- Planner protocol reuse of the existing worker transport ---------------

def test_worker_adapter_planner_reuses_existing_protocol_validation():
    params = {"goal": "Do it", "affected_files": [], "ambiguities": []}
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=WorkerToolCall(
            tool=TOOL_NAME, params=params,
        )),
    ])
    planner = WorkerAdapterPlanner(adapter, task_id="plan-x")
    response = planner.plan(PlannerRequest(original_request="Do it"))
    assert response.outcome == PlannerOutcome.STRUCTURED
    assert response.output.goal == "Do it"


def test_worker_adapter_planner_rejects_textual_tool_protocol_leakage():
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="<tool_call>{}</tool_call>"),
    ])
    planner = WorkerAdapterPlanner(adapter, task_id="plan-x")
    response = planner.plan(PlannerRequest(original_request="Do it"))
    assert response.outcome == PlannerOutcome.MALFORMED


def test_worker_adapter_planner_rejects_wrong_tool_name():
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=WorkerToolCall(
            tool="write_file", params={"path": "x", "content": "y"},
        )),
    ])
    planner = WorkerAdapterPlanner(adapter, task_id="plan-x")
    response = planner.plan(PlannerRequest(original_request="Do it"))
    assert response.outcome == PlannerOutcome.MALFORMED


def test_fake_planner_exhaustion_raises_not_silently_repeats():
    planner = FakePlanner([])
    with pytest.raises(FakePlannerError):
        planner.plan(PlannerRequest(original_request="x"))
