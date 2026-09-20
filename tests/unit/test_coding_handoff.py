"""`coding.handoff.validate_planner_handoff()`: the ONLY legitimate way a
`coding.contracts.ValidatedPlanReference` is constructed. Fails closed
with a stable `insufficient_coder_scope:<reason>` for every insufficient
plan shape -- never a bare `False`/`None`, and never trusts a
caller-supplied `EngineeringPlanContent` (always re-read and
hash-verified from durable storage).
"""

from __future__ import annotations

import pytest

from code_slayer.coding.handoff import validate_mutation_scope, validate_planner_handoff
from code_slayer.coding.pipeline_types import PipelineContractError
from code_slayer.intelligence.models import FileRecord, Snapshot
from code_slayer.planning.evidence import validate_plan_against_intelligence
from code_slayer.planning.models import AffectedFile, EngineeringPlanContent, PlannedChange
from code_slayer.planning.planner import (
    PlannerAffectedFileProposal,
    PlannerChangeProposal,
    PlannerStructuredOutput,
)
from code_slayer.planning.provenance import store_plan_content
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.planning_repo import PlanningRepo
from code_slayer.tools import file_tools as files


def _create_plan(conn, *, state, with_content):
    now = utcnow_iso()
    with transaction(conn):
        PlanningRepo(conn).create_in_transaction(
            plan_id="p1", created_at=now, schema_version="v1", repo_id="r1", worktree_id="w1",
            run_id=None, request_content_hash=files.digest(b"req"), predecessor_plan_id=None,
            revision=1, state="DRAFT",
        )
    plan_content_hash = None
    if with_content:
        content = EngineeringPlanContent(
            goal="do it", planned_changes=(PlannedChange("do a thing"),),
        )
        blob = store_plan_content(ContentStore(conn, "unused"), content)
        plan_content_hash = blob.content_hash
    with transaction(conn):
        PlanningRepo(conn).update_in_transaction(
            "p1", updated_at=utcnow_iso(), state=state, plan_content_hash=plan_content_hash,
        )
    return PlanningRepo(conn).get("p1")


@pytest.fixture
def blobs_dir(tmp_path):
    return tmp_path / "blobs"


def test_ready_plan_with_content_validates(db_conn, blobs_dir):
    now = utcnow_iso()
    with transaction(db_conn):
        PlanningRepo(db_conn).create_in_transaction(
            plan_id="p1", created_at=now, schema_version="v1", repo_id="r1", worktree_id="w1",
            run_id=None, request_content_hash=files.digest(b"req"), predecessor_plan_id=None,
            revision=1, state="DRAFT",
        )
    content = EngineeringPlanContent(goal="do it", planned_changes=(PlannedChange("step 1"),))
    blob = store_plan_content(ContentStore(db_conn, blobs_dir), content)
    with transaction(db_conn):
        PlanningRepo(db_conn).update_in_transaction(
            "p1", updated_at=utcnow_iso(), state="READY", plan_content_hash=blob.content_hash,
        )
    plan = PlanningRepo(db_conn).get("p1")
    validated = validate_planner_handoff(db_conn, blobs_dir, plan)
    assert validated.plan_id == "p1"
    assert validated.content_hash == blob.content_hash
    assert validated.content.goal == "do it"


@pytest.mark.parametrize("state", ["DRAFT", "NEEDS_INPUT", "SUPERSEDED"])
def test_non_ready_plan_is_rejected(db_conn, blobs_dir, state):
    plan = _create_plan(db_conn, state=state, with_content=False)
    with pytest.raises(PipelineContractError, match="insufficient_coder_scope:plan_not_ready"):
        validate_planner_handoff(db_conn, blobs_dir, plan)


def test_ready_plan_with_no_content_hash_is_rejected(db_conn, blobs_dir):
    plan = _create_plan(db_conn, state="READY", with_content=False)
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:missing_plan_content",
    ):
        validate_planner_handoff(db_conn, blobs_dir, plan)


def test_ready_plan_with_unreadable_content_is_rejected(db_conn, blobs_dir):
    now = utcnow_iso()
    with transaction(db_conn):
        PlanningRepo(db_conn).create_in_transaction(
            plan_id="p2", created_at=now, schema_version="v1", repo_id="r1", worktree_id="w1",
            run_id=None, request_content_hash=files.digest(b"req"), predecessor_plan_id=None,
            revision=1, state="DRAFT",
        )
        PlanningRepo(db_conn).update_in_transaction(
            "p2", updated_at=now, state="READY", plan_content_hash="a" * 64,
        )
    plan = PlanningRepo(db_conn).get("p2")
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:plan_content_unverifiable",
    ):
        validate_planner_handoff(db_conn, blobs_dir, plan)


def test_ready_plan_with_no_planned_changes_is_rejected(db_conn, blobs_dir):
    now = utcnow_iso()
    with transaction(db_conn):
        PlanningRepo(db_conn).create_in_transaction(
            plan_id="p3", created_at=now, schema_version="v1", repo_id="r1", worktree_id="w1",
            run_id=None, request_content_hash=files.digest(b"req"), predecessor_plan_id=None,
            revision=1, state="DRAFT",
        )
    content = EngineeringPlanContent(goal="do it", planned_changes=())
    blob = store_plan_content(ContentStore(db_conn, blobs_dir), content)
    with transaction(db_conn):
        PlanningRepo(db_conn).update_in_transaction(
            "p3", updated_at=utcnow_iso(), state="READY", plan_content_hash=blob.content_hash,
        )
    plan = PlanningRepo(db_conn).get("p3")
    with pytest.raises(PipelineContractError, match="insufficient_coder_scope:no_planned_changes"):
        validate_planner_handoff(db_conn, blobs_dir, plan)


def test_not_a_plan_row_is_rejected(db_conn, blobs_dir):
    with pytest.raises(PipelineContractError, match="insufficient_coder_scope:not_a_plan_row"):
        validate_planner_handoff(db_conn, blobs_dir, {"state": "READY"})


def _affected(path: str, action: str, *, exists: bool = True) -> AffectedFile:
    return AffectedFile(path=path, action=action, reason="test", exists_in_repository=exists)


def _content(*affected_files: AffectedFile) -> EngineeringPlanContent:
    return EngineeringPlanContent(goal="do it", affected_files=tuple(affected_files))


def test_validate_mutation_scope_accepts_exact_planner_authorized_paths():
    content = _content(
        _affected("src/foo.py", "modify"), _affected("tests/test_foo.py", "create", exists=False),
    )
    validate_mutation_scope(content, ("src/foo.py", "tests/test_foo.py"))  # no raise


def test_validate_mutation_scope_accepts_a_narrower_subset():
    """Planner authorizes src/foo.py and tests/test_foo.py; caller
    supplies only src/foo.py -- an acceptable narrower scope (this fix's
    own worked example)."""
    content = _content(
        _affected("src/foo.py", "modify"), _affected("tests/test_foo.py", "create", exists=False),
    )
    validate_mutation_scope(content, ("src/foo.py",))  # no raise


def test_validate_mutation_scope_rejects_a_broader_prefix():
    """Planner authorizes src/foo.py; caller supplies ('.',) -- reject
    fail-closed, exactly this fix's own worked example."""
    content = _content(_affected("src/foo.py", "modify"))
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:scope_exceeds_planner_authorization",
    ):
        validate_mutation_scope(content, (".",))


def test_validate_mutation_scope_rejects_any_path_not_planner_authorized():
    content = _content(_affected("src/foo.py", "modify"))
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:scope_exceeds_planner_authorization",
    ):
        validate_mutation_scope(content, ("src/foo.py", "src/bar.py"))


def test_validate_mutation_scope_excludes_inspect_only_paths():
    """Planner authorizes MODIFY src/foo.py and INSPECT src/context.py
    (read for context only); caller requests src/context.py -- reject
    fail-closed, exactly this fix's own worked example: INSPECT never
    authorizes mutation."""
    content = _content(
        _affected("src/context.py", "inspect"), _affected("src/foo.py", "modify"),
    )
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:scope_exceeds_planner_authorization",
    ):
        validate_mutation_scope(content, ("src/context.py",))
    validate_mutation_scope(content, ("src/foo.py",))  # the MODIFY path alone is fine


def test_validate_mutation_scope_fails_closed_when_plan_names_no_paths_at_all():
    """A READY plan can have empty `affected_files`, or only `INSPECT`
    entries -- with no safe mutation authorization to derive scope from,
    every non-empty request must fail closed, never silently accepted as
    "no restriction"."""
    content = _content()
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:scope_exceeds_planner_authorization",
    ):
        validate_mutation_scope(content, ("anything.py",))

    inspect_only = _content(_affected("src/context.py", "inspect"))
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:scope_exceeds_planner_authorization",
    ):
        validate_mutation_scope(inspect_only, ("src/context.py",))


def test_validate_mutation_scope_derives_from_affected_files_not_planned_changes():
    """The exact bug this fix closes: a `planned_changes[].paths` claim
    (the Planner model's own unvalidated text) must NEVER be treated as
    authorization -- only `affected_files` (repository-evidence-
    validated) counts, even when `planned_changes` claims something
    completely different."""
    content = EngineeringPlanContent(
        goal="do it",
        planned_changes=(PlannedChange(description="misleading claim", paths=(".",)),),
        affected_files=(_affected("src/foo.py", "modify"),),
    )
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:scope_exceeds_planner_authorization",
    ):
        validate_mutation_scope(content, (".",))
    validate_mutation_scope(content, ("src/foo.py",))  # no raise


def test_validate_mutation_scope_rejects_traversal_and_absolute_scope_entries():
    content = _content(_affected("src/foo.py", "modify"))
    for malformed in ("../etc/passwd", "/etc/passwd", "src/../../../etc/passwd"):
        with pytest.raises(
            PipelineContractError, match="insufficient_coder_scope:malformed_scope_path",
        ):
            validate_mutation_scope(content, (malformed,))


def test_validate_mutation_scope_excludes_unsafe_affected_file_paths_rather_than_crashing():
    """A malformed/unsafe `affected_files` path (traversal, absolute --
    `planning.evidence` only checks EXISTENCE, never path shape) can
    never legitimately authorize anything; it is excluded from the
    authorized set, not treated as a crash or as accidentally granting
    scope."""
    content = _content(
        _affected("../../../etc/passwd", "create", exists=False),
        _affected("src/foo.py", "modify"),
    )
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:malformed_scope_path",
    ):
        validate_mutation_scope(content, ("../../../etc/passwd",))
    validate_mutation_scope(content, ("src/foo.py",))  # no raise


def test_validate_mutation_scope_deduplicates_repeated_paths():
    content = _content(_affected("src/foo.py", "modify"))
    validate_mutation_scope(content, ("src/foo.py", "src/foo.py"))  # no raise


def _snapshot(*files_: FileRecord) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-1", repo_id="r1", worktree_id="w1", head_sha="deadbeef",
        branch="main", working_tree_dirty=False, working_tree_fingerprint="fp",
        index_version="v1", created_at=utcnow_iso(), files=files_, inventory_truncated=False,
        projects=(), commands=(), symbols=(), symbol_errors=(), edges=(), indexed_text_bytes=0,
    )


def _file(path: str) -> FileRecord:
    return FileRecord(path=path, language="python", size=10, tracked=True, classification="source")


def test_real_evidence_path_rejects_hostile_planned_changes_scope_claim():
    """Does NOT hand-construct `EngineeringPlanContent` -- builds a real
    `PlannerStructuredOutput` (exactly what a live Planner turn would
    produce), runs it through `planning.evidence.
    validate_plan_against_intelligence()` (the actual, unmodified
    repository-evidence validation this codebase already uses to decide
    what reaches `READY`), and calls `validate_mutation_scope()` on the
    genuinely evidence-validated result.

    The hostile/misaligned case this fix exists for: the raw Planner
    proposal's `planned_changes[].paths` claims `(".",)` -- a completely
    different, much broader claim than what its own `affected_files`
    entry (the one field `planning.evidence` actually checks against the
    repository snapshot) authorizes. `allowed_scope=(".",)` must be
    rejected; `allowed_scope=("src/foo.py",)` -- the genuinely
    evidence-validated path -- must be accepted."""
    snapshot = _snapshot(_file("src/foo.py"))
    output = PlannerStructuredOutput(
        goal="fix the foo bug",
        affected_files=(
            PlannerAffectedFileProposal(path="src/foo.py", action="modify", reason="fix the bug"),
        ),
        # The hostile/misaligned claim: this text is entirely the
        # Planner's own, and `planning.evidence` never checks it against
        # anything -- confirmed below it survives into `content`
        # unchanged, and confirmed `validate_mutation_scope()` still
        # correctly ignores it.
        planned_changes=(
            PlannerChangeProposal(description="rewrite everything", paths=(".",)),
        ),
    )
    result = validate_plan_against_intelligence(output, snapshot)
    assert not result.blocking, result.issues
    content = result.content

    # The evidence-validated field really was checked against the
    # repository snapshot (exists_in_repository derived from `snapshot.
    # files`, never from the Planner's own claim).
    assert len(content.affected_files) == 1
    assert content.affected_files[0].path == "src/foo.py"
    assert content.affected_files[0].exists_in_repository is True

    # The unvalidated field really does still carry the hostile claim
    # unchanged -- planning.evidence corrects/drops other claim kinds,
    # but never touches planned_changes.paths at all.
    assert content.planned_changes[0].paths == (".",)

    # The hostile claim must not authorize anything.
    with pytest.raises(
        PipelineContractError, match="insufficient_coder_scope:scope_exceeds_planner_authorization",
    ):
        validate_mutation_scope(content, (".",))

    # The genuinely evidence-validated path is accepted.
    validate_mutation_scope(content, ("src/foo.py",))  # no raise
