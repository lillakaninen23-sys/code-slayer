"""Planner-turn context budget (Phase 8.2b).

A live production planning turn against a real local model sent a
durable planner input of 151211 bytes -- far beyond what any single
planning turn needs -- and the model, faced with that much prompt
content, ignored the required structured `emit_engineering_plan` tool
and emitted free-form prose instead. These tests prove the fix: a
planner turn's own context is bounded by explicit, centralized,
deterministic `planning.limits` constants (never the broader
Repository Intelligence indexing/query defaults), relevance-ranked
(top-ranked evidence wins when budget-constrained, never arbitrary
files), and never duplicates discovered commands as file context.
"""

from __future__ import annotations

import pytest

from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.limits import (
    PLANNER_MAX_FILES,
    PLANNER_MAX_PER_FILE_BYTES,
    PLANNER_MAX_TOTAL_FILE_BYTES,
)
from code_slayer.planning.planner import (
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    parse_planner_output,
    render_bounded_context,
)
from code_slayer.planning.service import EngineeringPlanningService
from tests.repo_helpers import git

REQUEST = "Add a read-only API endpoint that reports repository intelligence snapshot age."


def _structured_response(**overrides) -> PlannerResponse:
    data = {
        "goal": "Add the endpoint", "requirements": [], "assumptions": [], "affected_files": [],
        "planned_changes": [], "dependencies": [], "risks": [], "verification_steps": [],
        "discovered_commands": [], "authority_requirements": [], "evidence_claims": [],
        "ambiguities": [],
    }
    data.update(overrides)
    output = parse_planner_output(data)
    assert output is not None
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


# --- 1/2/3. explicit planner-turn budget, never the indexing defaults ------

def test_planner_context_obeys_max_files(service, git_repo_with_commit):
    for i in range(20):
        (git_repo_with_commit / f"module_{i:02d}.py").write_text(
            "REQUEST_MENTIONS_THIS_MODULE = True\n" + ("# padding\n" * 50),
        )
    request_mentioning_all = " ".join(f"module_{i:02d}" for i in range(20))
    planner = FakePlanner([_structured_response()])
    service.create(original_request=f"Inspect {request_mentioning_all}", planner=planner)
    request = planner.calls[0]
    assert request.context_pack is not None
    assert len(request.context_pack.files) <= PLANNER_MAX_FILES


def test_planner_context_obeys_total_byte_budget(service, git_repo_with_commit):
    # Ten files, each comfortably under the per-file cap on its own, but
    # ten of them together would exceed the total budget many times over.
    for i in range(10):
        (git_repo_with_commit / f"module_{i:02d}.py").write_text("x" * 5000)
    request_mentioning_all = " ".join(f"module_{i:02d}" for i in range(10))
    planner = FakePlanner([_structured_response()])
    service.create(original_request=f"Inspect {request_mentioning_all}", planner=planner)
    request = planner.calls[0]
    total = sum(len(f.content.encode("utf-8")) for f in request.context_pack.files)
    assert total <= PLANNER_MAX_TOTAL_FILE_BYTES


def test_planner_context_obeys_per_file_byte_budget(service, git_repo_with_commit):
    (git_repo_with_commit / "huge_module.py").write_text("x" * 100_000)
    planner = FakePlanner([_structured_response()])
    service.create(original_request="Inspect huge_module thoroughly", planner=planner)
    request = planner.calls[0]
    matching = [f for f in request.context_pack.files if f.path == "huge_module.py"]
    assert matching, "expected the exact-name-matched file to be selected at all"
    assert len(matching[0].content.encode("utf-8")) <= PLANNER_MAX_PER_FILE_BYTES
    assert matching[0].truncated is True


def test_planner_limits_are_significantly_below_the_observed_failure(git_repo_with_commit):
    observed_failure_bytes = 151211
    assert PLANNER_MAX_TOTAL_FILE_BYTES * 4 < observed_failure_bytes


# --- 4. top-ranked relevant files win over low-ranked files -----------------

def test_top_ranked_files_win_over_low_ranked_files_when_budget_constrained(
    service, git_repo_with_commit,
):
    (git_repo_with_commit / "billing.py").write_text("def charge():\n    pass\n")
    for i in range(6):
        (git_repo_with_commit / f"importer_{i}.py").write_text("import billing\n")
    for i in range(5):
        (git_repo_with_commit / f"test_low_rank_{i}.py").write_text("X = 1\n")
    planner = FakePlanner([_structured_response()])
    service.create(
        original_request="Inspect billing.py and its tests thoroughly", planner=planner,
    )
    request = planner.calls[0]
    selected = {f.path for f in request.context_pack.files}
    assert len(selected) <= PLANNER_MAX_FILES
    importer_paths = {f"importer_{i}.py" for i in range(6)}
    low_rank_paths = {f"test_low_rank_{i}.py" for i in range(5)}
    # 12 candidates score above zero (billing.py:100, 6 importers:20
    # each, 5 test-keyword files:10 each); with an 8-file budget, the
    # top 8 by rank must be exactly billing.py plus all 6 importers plus
    # only 1 of the 5 lower-ranked test-keyword files -- never a lower-
    # ranked file selected while a higher-ranked one was dropped.
    assert selected == {"billing.py"} | importer_paths | (selected & low_rank_paths)
    assert len(selected & low_rank_paths) <= 1


# --- 5. commands are not duplicated as arbitrary file context ---------------

def test_commands_are_not_duplicated_in_context_pack_rendering(service, git_repo_with_commit):
    (git_repo_with_commit / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    planner = FakePlanner([_structured_response()])
    service.create(original_request=REQUEST, planner=planner)
    request = planner.calls[0]
    rendered = render_bounded_context(request)
    assert rendered["context_pack"] is None or "commands" not in rendered["context_pack"]
    assert rendered["context_pack"] is None or "projects" not in rendered["context_pack"]
    # discovered_commands appears exactly once, at the top level.
    assert isinstance(rendered["discovered_commands"], list)


def test_render_bounded_context_never_duplicates_project_or_command_evidence():
    from code_slayer.intelligence.models import (
        CommandCandidate,
        ContextFile,
        ContextPack,
        ProjectEvidence,
    )

    project = ProjectEvidence(kind="python", evidence_paths=("pyproject.toml",), facts={})
    command = CommandCandidate("pytest", "test", "pyproject.toml:[tool.pytest]", "high")
    pack = ContextPack(
        snapshot_id="snap1", repo_id="r", worktree_id="w", head_sha="abc", query="x",
        files=(ContextFile("a.py", "content", False, ("reason",)),),
        projects=(project,), commands=(command,), omitted=("b.py", "c.py"),
        budget_exhausted=True, stale=False,
    )
    request = PlannerRequest(
        original_request="x", repo_context=(project,), discovered_commands=(command,),
        context_pack=pack,
    )
    rendered = render_bounded_context(request)
    assert rendered["repo_context"] == [
        {"kind": "python", "evidence_paths": ("pyproject.toml",), "facts": {}},
    ]
    assert len(rendered["discovered_commands"]) == 1
    assert "commands" not in rendered["context_pack"]
    assert "projects" not in rendered["context_pack"]
    assert rendered["context_pack"]["files"][0]["path"] == "a.py"
    assert rendered["context_pack"]["omitted_count"] == 2
    assert "omitted" not in rendered["context_pack"]  # full path list dropped, count retained


# --- 6/7. no repository mutation, no command execution ----------------------

def test_bounded_planning_performs_no_repository_mutation(service, git_repo_with_commit):
    (git_repo_with_commit / "module_x.py").write_text("x" * 5000)
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    before_bytes = (git_repo_with_commit / "module_x.py").read_bytes()
    planner = FakePlanner([_structured_response()])
    service.create(original_request="Inspect module_x", planner=planner)
    assert git(git_repo_with_commit, "status", "--porcelain") == before_status
    assert (git_repo_with_commit / "module_x.py").read_bytes() == before_bytes


def test_bounded_planning_never_executes_discovered_commands(service, git_repo_with_commit):
    (git_repo_with_commit / "Makefile").write_text("test:\n\ttouch executed.marker\n")
    planner = FakePlanner([_structured_response(discovered_commands=[
        {"command": "make test", "purpose": "test", "evidence_source": "fabricated"},
    ])])
    service.create(original_request=REQUEST, planner=planner)
    assert not (git_repo_with_commit / "executed.marker").exists()
