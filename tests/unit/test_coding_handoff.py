"""`coding.handoff.validate_planner_handoff()`: the ONLY legitimate way a
`coding.contracts.ValidatedPlanReference` is constructed. Fails closed
with a stable `insufficient_coder_scope:<reason>` for every insufficient
plan shape -- never a bare `False`/`None`, and never trusts a
caller-supplied `EngineeringPlanContent` (always re-read and
hash-verified from durable storage).
"""

from __future__ import annotations

import pytest

from code_slayer.coding.handoff import validate_planner_handoff
from code_slayer.coding.pipeline_types import PipelineContractError
from code_slayer.planning.models import EngineeringPlanContent, PlannedChange
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
