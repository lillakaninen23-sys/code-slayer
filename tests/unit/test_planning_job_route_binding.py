"""Schema v19 (H.4): `planning_jobs`' durable Planner route binding.

Covers the migration itself (v18 -> v19 succeeds, historical rows
preserved with NULL binding fields, a new insert requires a complete
binding), the binding's own immutability after creation, and H.4's
integration of worker-bound planning work into `workers.lifecycle`'s
archive active-work policy.
"""

from __future__ import annotations

import sqlite3

import pytest

from code_slayer.store import db as db_module
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.planning_jobs_repo import PlanningJobsRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.lifecycle import archive_worker

WORKER_ID = "w1"


def _apply_through(conn: sqlite3.Connection, version: int) -> None:
    """Mirrors `test_promotion_provenance_migration._apply_through`:
    apply every known migration up to (and including) `version` only."""
    current = db_module.schema_version(conn)
    for mig_version, _name, sql in db_module._discover_migrations():
        if mig_version <= current or mig_version > version:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (mig_version, utcnow_iso()),
        )
        conn.execute("COMMIT")


def _insert_v18_plan_and_job(conn, *, plan_id: str, job_id: str) -> None:
    """Raw INSERT matching the schema-v18 (pre-H.4) `planning_jobs`
    column set -- no route-binding columns exist yet at this schema
    version."""
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO engineering_plans "
        "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
        " request_content_hash, revision, state) "
        "VALUES (?, ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
        (plan_id, now, now),
    )
    conn.execute(
        "INSERT INTO planning_jobs "
        "(job_id, plan_id, repo_id, worktree_id, created_at, updated_at, kind, state, "
        " attempt, owner_generation) "
        "VALUES (?, ?, 'repo-1', 'wt-1', ?, ?, 'create', 'QUEUED', 0, 0)",
        (job_id, plan_id, now, now),
    )


def _real_route_binding_kwargs(worker_id: str = WORKER_ID) -> dict:
    return dict(
        worker_id=worker_id, runtime_identity_fingerprint="rf-1",
        role_evaluation_fingerprint="ef-1", security_certificate_id="sec-1",
        role_certificate_id="role-1", output_token_budget=4096,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        planner_timeout_seconds=45.0,
        planner_policy_version="planner-certification-v1",
    )


# -- migration ----------------------------------------------------------


def test_v18_to_v19_upgrade_succeeds_and_historical_job_gets_null_binding(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 18)
    assert db_module.schema_version(conn) == 18
    conn.execute("BEGIN IMMEDIATE")
    _insert_v18_plan_and_job(conn, plan_id="plan-1", job_id="job-1")
    conn.execute("COMMIT")

    _apply_through(conn, 19)
    assert db_module.schema_version(conn) == 19

    row = conn.execute("SELECT * FROM planning_jobs WHERE job_id = 'job-1'").fetchone()
    assert row["worker_id"] is None
    assert row["runtime_identity_fingerprint"] is None
    assert row["role_evaluation_fingerprint"] is None
    assert row["security_certificate_id"] is None
    assert row["role_certificate_id"] is None
    assert row["output_token_budget"] is None
    assert row["tool_choice_enforcement"] is None
    assert row["planner_policy_version"] is None
    # Every pre-existing identity/state field is byte-for-byte unchanged.
    assert row["state"] == "QUEUED"
    assert row["kind"] == "create"
    conn.close()


def test_v18_to_v19_upgrade_preserves_multiple_historical_jobs_unchanged(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 18)
    conn.execute("BEGIN IMMEDIATE")
    _insert_v18_plan_and_job(conn, plan_id="plan-1", job_id="job-1")
    _insert_v18_plan_and_job(conn, plan_id="plan-2", job_id="job-2")
    conn.execute("COMMIT")

    before = {
        jid: dict(conn.execute("SELECT * FROM planning_jobs WHERE job_id = ?", (jid,)).fetchone())
        for jid in ("job-1", "job-2")
    }
    _apply_through(conn, 19)
    assert db_module.schema_version(conn) == 19
    after = {
        jid: dict(conn.execute("SELECT * FROM planning_jobs WHERE job_id = ?", (jid,)).fetchone())
        for jid in ("job-1", "job-2")
    }
    for jid in ("job-1", "job-2"):
        for key in before[jid]:
            assert after[jid][key] == before[jid][key], (jid, key)
    conn.close()


def _insert_v19_plan_and_bound_job(conn, *, plan_id: str, job_id: str, worker_id: str) -> None:
    """Raw INSERT matching the schema-v19 (H.4, pre-H.4.1) `planning_jobs`
    column set -- no `planner_timeout_seconds` column exists yet at this
    schema version, but every other route-binding field is complete."""
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO engineering_plans "
        "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
        " request_content_hash, revision, state) "
        "VALUES (?, ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
        (plan_id, now, now),
    )
    conn.execute(
        "INSERT INTO planning_jobs "
        "(job_id, plan_id, repo_id, worktree_id, created_at, updated_at, kind, state, "
        " attempt, owner_generation, worker_id, runtime_identity_fingerprint, "
        " role_evaluation_fingerprint, security_certificate_id, role_certificate_id, "
        " output_token_budget, tool_choice_enforcement, planner_policy_version) "
        "VALUES (?, ?, 'repo-1', 'wt-1', ?, ?, 'create', 'QUEUED', 0, 0, "
        " ?, 'rf-1', 'ef-1', 'sec-1', 'role-1', 4096, 'ADVISORY_ONLY_UNVERIFIED', "
        " 'planner-certification-v1')",
        (job_id, plan_id, now, now, worker_id),
    )


def test_v19_to_v20_upgrade_succeeds_and_historical_bound_job_gets_null_timeout(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 19)
    assert db_module.schema_version(conn) == 19
    WorkersRepo(conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    conn.execute("BEGIN IMMEDIATE")
    _insert_v19_plan_and_bound_job(conn, plan_id="plan-1", job_id="job-1", worker_id=WORKER_ID)
    conn.execute("COMMIT")

    assert db_module.migrate(conn) == db_module.known_schema_version()

    row = conn.execute("SELECT * FROM planning_jobs WHERE job_id = 'job-1'").fetchone()
    # A fully-bound v19 row is never rewritten to claim a timeout it
    # never actually had -- `planner_timeout_seconds` alone lands NULL,
    # while every original H.4 binding field is untouched.
    assert row["planner_timeout_seconds"] is None
    assert row["worker_id"] == WORKER_ID
    assert row["output_token_budget"] == 4096
    assert row["planner_policy_version"] == "planner-certification-v1"
    conn.close()


def test_v20_new_row_via_repo_requires_complete_binding(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    assert db_module.migrate(conn) == db_module.known_schema_version()
    WorkersRepo(conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    now = utcnow_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO engineering_plans "
            "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
            " request_content_hash, revision, state) "
            "VALUES ('plan-1', ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
            (now, now),
        )
        row = PlanningJobsRepo(conn).create_in_transaction(
            job_id="job-1", plan_id="plan-1", repo_id="repo-1", worktree_id="wt-1",
            created_at=now, kind="create", **_real_route_binding_kwargs(),
        )
    assert row.worker_id == WORKER_ID
    assert row.output_token_budget == 4096
    assert row.planner_timeout_seconds == 45.0
    conn.close()


def test_v20_raw_insert_missing_planner_timeout_seconds_is_rejected_by_trigger(tmp_path):
    """H.4.1: the extended completeness trigger refuses a new row that
    has every original H.4 field but omits the new ninth field alone."""
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    assert db_module.migrate(conn) == db_module.known_schema_version()
    WorkersRepo(conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO engineering_plans "
        "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
        " request_content_hash, revision, state) "
        "VALUES ('plan-1', ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
        (now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO planning_jobs "
            "(job_id, plan_id, repo_id, worktree_id, created_at, updated_at, kind, state, "
            " attempt, owner_generation, worker_id, runtime_identity_fingerprint, "
            " role_evaluation_fingerprint, security_certificate_id, role_certificate_id, "
            " output_token_budget, tool_choice_enforcement, planner_policy_version, "
            " planner_timeout_seconds) "
            "VALUES ('job-1', 'plan-1', 'repo-1', 'wt-1', ?, ?, 'create', 'QUEUED', 0, 0, "
            " 'w1', 'rf-1', 'ef-1', 'sec-1', 'role-1', 4096, 'ADVISORY_ONLY_UNVERIFIED', "
            " 'planner-certification-v1', NULL)",
            (now, now),
        )
    conn.execute("ROLLBACK")
    conn.close()


def test_v19_raw_insert_missing_route_binding_field_is_rejected_by_trigger(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    assert db_module.migrate(conn) == db_module.known_schema_version()
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO engineering_plans "
        "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
        " request_content_hash, revision, state) "
        "VALUES ('plan-1', ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
        (now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO planning_jobs "
            "(job_id, plan_id, repo_id, worktree_id, created_at, updated_at, kind, state, "
            " attempt, owner_generation, worker_id, runtime_identity_fingerprint, "
            " role_evaluation_fingerprint, security_certificate_id, role_certificate_id, "
            " output_token_budget, tool_choice_enforcement, planner_policy_version) "
            "VALUES ('job-1', 'plan-1', 'repo-1', 'wt-1', ?, ?, 'create', 'QUEUED', 0, 0, "
            " 'w1', 'rf-1', 'ef-1', 'sec-1', 'role-1', 4096, 'ADVISORY_ONLY_UNVERIFIED', NULL)",
            (now, now),
        )
    conn.execute("ROLLBACK")
    conn.close()


# -- immutability ---------------------------------------------------------


@pytest.mark.parametrize("column,new_value", [
    ("worker_id", "w2"),
    ("runtime_identity_fingerprint", "different-fp"),
    ("role_evaluation_fingerprint", "different-ef"),
    ("security_certificate_id", "different-sec"),
    ("role_certificate_id", "different-role"),
    ("output_token_budget", 8192),
    ("tool_choice_enforcement", "HARD_ENFORCED"),
    ("planner_policy_version", "planner-certification-v3"),
    ("planner_timeout_seconds", 300.0),
])
def test_route_binding_field_is_immutable_after_creation(tmp_path, column, new_value):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    db_module.migrate(conn)
    WorkersRepo(conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    now = utcnow_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO engineering_plans "
            "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
            " request_content_hash, revision, state) "
            "VALUES ('plan-1', ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
            (now, now),
        )
        PlanningJobsRepo(conn).create_in_transaction(
            job_id="job-1", plan_id="plan-1", repo_id="repo-1", worktree_id="wt-1",
            created_at=now, kind="create", **_real_route_binding_kwargs(),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(
                f"UPDATE planning_jobs SET {column} = ? WHERE job_id = 'job-1'",
                (new_value,),
            )
    row = conn.execute("SELECT * FROM planning_jobs WHERE job_id = 'job-1'").fetchone()
    assert row[column] != new_value
    conn.close()


def test_route_binding_survives_ordinary_lifecycle_updates(tmp_path):
    """Non-identity fields (state/attempt/owner_*/...) still update
    freely -- the trigger constrains only the eight H.4 fields, exactly
    like it already did for the pre-existing identity fields."""
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    db_module.migrate(conn)
    WorkersRepo(conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    now = utcnow_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO engineering_plans "
            "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
            " request_content_hash, revision, state) "
            "VALUES ('plan-1', ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
            (now, now),
        )
        PlanningJobsRepo(conn).create_in_transaction(
            job_id="job-1", plan_id="plan-1", repo_id="repo-1", worktree_id="wt-1",
            created_at=now, kind="create", **_real_route_binding_kwargs(),
        )
        claimed = PlanningJobsRepo(conn).claim_in_transaction(
            "job-1", owner_pid=1234, owner_pid_started_at=None, now=now,
        )
    assert claimed.state == "RUNNING"
    assert claimed.worker_id == WORKER_ID  # unchanged throughout
    conn.close()


# -- H.4: planning_jobs integrated into H.3's archive active-work policy ----


@pytest.fixture
def registered(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    db_module.migrate(conn)
    WorkersRepo(conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    yield conn
    conn.close()


def _seed_job(conn, *, job_id: str, state: str, worker_id: str = WORKER_ID) -> None:
    """Seed a real, H.4-complete `planning_jobs` row bound to
    `worker_id`, in `state`. A legacy NULL-worker row is a distinct,
    pre-H.4 shape only reachable via a v18-frozen raw INSERT + upgrade
    (see `test_legacy_null_worker_job_blocks_no_specific_worker`) --
    never something a post-v19 caller can construct at all (the
    completeness trigger forbids it), so this helper has no such mode."""
    now = utcnow_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO engineering_plans "
            "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, "
            " request_content_hash, revision, state) "
            f"VALUES ('plan-{job_id}', ?, ?, 'test-v1', 'repo-1', 'wt-1', 'hash-1', 1, 'DRAFT')",
            (now, now),
        )
        PlanningJobsRepo(conn).create_in_transaction(
            job_id=job_id, plan_id=f"plan-{job_id}", repo_id="repo-1", worktree_id="wt-1",
            created_at=now, kind="create", **_real_route_binding_kwargs(worker_id),
        )
    if state != "QUEUED":
        with transaction(conn):
            claimed = PlanningJobsRepo(conn).claim_in_transaction(
                job_id, owner_pid=1234, owner_pid_started_at=None, now=utcnow_iso(),
            )
            if state != "RUNNING":
                PlanningJobsRepo(conn).finish_in_transaction(
                    job_id, state=state, expected_generation=claimed.owner_generation,
                    now=utcnow_iso(),
                )


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING"])
def test_archive_refuses_with_blocking_planning_job(registered, state):
    _seed_job(registered, job_id="job-1", state=state)
    result = archive_worker(registered, worker_id=WORKER_ID)
    assert result.ok is False
    assert result.reason == "worker_has_active_work"


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED"])
def test_archive_allowed_with_only_terminal_planning_jobs(registered, state):
    _seed_job(registered, job_id="job-1", state=state)
    result = archive_worker(registered, worker_id=WORKER_ID)
    assert result.ok is True
    assert result.changed is True


def test_legacy_null_worker_job_blocks_no_specific_worker(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 18)
    conn.execute("BEGIN IMMEDIATE")
    _insert_v18_plan_and_job(conn, plan_id="plan-1", job_id="job-1")
    conn.execute("COMMIT")
    assert db_module.migrate(conn) == db_module.known_schema_version()
    WorkersRepo(conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")

    row = conn.execute("SELECT * FROM planning_jobs WHERE job_id = 'job-1'").fetchone()
    assert row["worker_id"] is None
    assert row["state"] == "QUEUED"  # genuinely non-terminal, but bound to no worker

    result = archive_worker(conn, worker_id=WORKER_ID)
    assert result.ok is True
    assert result.changed is True
    conn.close()
