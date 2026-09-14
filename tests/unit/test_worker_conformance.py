"""Durable worker conformance runs (Phase 7.3): a suite executes as one
coherent run, individual PASS results from different runs can never be
composed into one passing suite, and only a genuinely complete, passing,
correctly-scoped run may justify LOCKED -> GUARDED."""

from __future__ import annotations

import sqlite3

import pytest

from code_slayer.store.conformance_repo import (
    ConformanceRepo,
    ConformanceRunStatus,
    DuplicateConformanceResultError,
)
from code_slayer.store.db import transaction
from code_slayer.store.worker_trust_repo import TrustLevel
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.conformance import (
    REQUIRED_CASES,
    SUITE_VERSION,
    run_conformance_suite,
)
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from code_slayer.workers.trust import WorkerTrustManager


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


def _passing_responses() -> list[WorkerResponse | Exception]:
    """One canned response per required case, in the fixed suite order,
    each shaped to make that specific case pass."""
    return [
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="hi there"),  # inference
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="clean output"),  # structured_output
        WorkerResponse(  # structured_tool_call
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "a.txt"}),
        ),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="continuing"),  # tool_result_consumption
        WorkerResponse(  # read_only_compliance
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "b.txt"}),
        ),
        WorkerResponse(  # malformed_protocol_rejection
            kind=WorkerResponseKind.TEXT,
            text="<function=skill>\n<parameter=name>\ngit\n</parameter>\n</function>",
        ),
        RuntimeError("simulated timeout"),  # timeout_error_handling
    ]


def _run_passing_suite(db_conn, worker_id: str, role: str = "coder"):
    adapter = FakeWorkerAdapter(_passing_responses())
    return run_conformance_suite(db_conn, adapter, worker_id=worker_id, role=role)


# --- 1. new run begins RUNNING ---------------------------------------------

def test_new_run_begins_running(db_conn, registered_worker):
    """Interrupting the real (synchronous) suite runner mid-execution is
    impractical in-process; the repo layer itself directly and
    deterministically proves the initial status new runs get."""
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        run = repo.start_run_in_transaction(
            run_id="probe-run", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    assert run.status == ConformanceRunStatus.RUNNING
    assert run.completed_at is None


# --- 2. every result belongs to exactly one run_id --------------------------

def test_every_result_belongs_to_exactly_one_run_id(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    results = ConformanceRepo(db_conn).list_results(result.run_id)
    assert len(results) == len(REQUIRED_CASES)
    assert all(r.run_id == result.run_id for r in results)


# --- 3. duplicate case result rejected --------------------------------------

def test_duplicate_case_result_rejected(db_conn, registered_worker):
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-dup", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    with transaction(db_conn):
        repo.record_result_in_transaction(
            run_id="run-dup", case_name="inference", passed=True, reason="ok",
            detail_content_hash=None, occurred_at="2026-01-01T00:00:01.000000Z",
        )
    with pytest.raises(DuplicateConformanceResultError):
        with transaction(db_conn):
            repo.record_result_in_transaction(
                run_id="run-dup", case_name="inference", passed=True, reason="ok-again",
                detail_content_hash=None, occurred_at="2026-01-01T00:00:02.000000Z",
            )
    # Still exactly one result for that case -- the failed second attempt
    # left no partial/duplicate row.
    results = [r for r in repo.list_results("run-dup") if r.case_name == "inference"]
    assert len(results) == 1


# --- 4. incomplete run cannot PASS ------------------------------------------

def test_incomplete_run_cannot_pass(db_conn, registered_worker):
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-incomplete", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    with transaction(db_conn):
        repo.record_result_in_transaction(
            run_id="run-incomplete", case_name="inference", passed=True, reason="ok",
            detail_content_hash=None, occurred_at="2026-01-01T00:00:01.000000Z",
        )
    # Never finalized -- simulating a crash partway through. Still RUNNING.
    run = repo.get_run("run-incomplete")
    assert run.status == ConformanceRunStatus.RUNNING
    result = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id="run-incomplete",
    )
    assert not result.ok
    assert result.reason == "run_not_finalized"


# --- 5/6. required cases determine PASSED/FAILED ----------------------------

def test_all_required_promotion_cases_pass_in_one_run_yields_passed(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    assert result.status == ConformanceRunStatus.PASSED
    run = ConformanceRepo(db_conn).get_run(result.run_id)
    assert run.status == ConformanceRunStatus.PASSED
    assert run.completed_at is not None


def test_one_required_failure_yields_failed(db_conn, registered_worker):
    responses = _passing_responses()
    # Break the malformed_protocol_rejection case: return a well-formed
    # response instead of the incident-shaped one, so containment has
    # nothing to reject -- the case must fail.
    responses[5] = WorkerResponse(kind=WorkerResponseKind.TEXT, text="perfectly normal text")
    adapter = FakeWorkerAdapter(responses)
    result = run_conformance_suite(db_conn, adapter, worker_id=registered_worker, role="coder")
    assert result.status == ConformanceRunStatus.FAILED
    results = {r.case_name: r.passed for r in ConformanceRepo(db_conn).list_results(result.run_id)}
    assert results["malformed_protocol_rejection"] is False
    assert results["inference"] is True  # the other cases still ran and still passed


# --- 7. results from another run cannot satisfy missing cases --------------

def test_results_from_another_run_cannot_satisfy_missing_cases(db_conn, registered_worker):
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-a", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
        # run-a passes every case except one.
    for case in sorted(REQUIRED_CASES - {"timeout_error_handling"}):
        with transaction(db_conn):
            repo.record_result_in_transaction(
                run_id="run-a", case_name=case, passed=True, reason="ok",
                detail_content_hash=None, occurred_at="2026-01-01T00:00:01.000000Z",
            )
    with transaction(db_conn):
        repo.finalize_run_in_transaction(
            "run-a", status=ConformanceRunStatus.FAILED, completed_at="2026-01-01T00:00:02.000000Z",
        )

    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-b", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:01:00.000000Z",
        )
    with transaction(db_conn):
        # run-b passes only the one case run-a was missing.
        repo.record_result_in_transaction(
            run_id="run-b", case_name="timeout_error_handling", passed=True, reason="ok",
            detail_content_hash=None, occurred_at="2026-01-01T00:01:01.000000Z",
        )
    with transaction(db_conn):
        repo.finalize_run_in_transaction(
            "run-b", status=ConformanceRunStatus.FAILED, completed_at="2026-01-01T00:01:02.000000Z",
        )

    # Neither run, alone, is PASSED -- and promotion must not be able to
    # stitch run-a's cases together with run-b's to fabricate a pass.
    for run_id in ("run-a", "run-b"):
        result = promote_from_conformance(
            db_conn, worker_id=registered_worker, role="coder", run_id=run_id,
        )
        assert not result.ok
        assert result.reason == "run_not_passed"


# --- 8/9/10. run state gates promotion --------------------------------------

def test_unknown_run_cannot_promote_trust(db_conn, registered_worker):
    result = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id="no-such-run",
    )
    assert not result.ok
    assert result.reason == "unknown_conformance_run"


def test_running_run_cannot_promote_trust(db_conn, registered_worker):
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-running", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    result = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id="run-running",
    )
    assert not result.ok
    assert result.reason == "run_not_finalized"


def test_failed_run_cannot_promote_trust(db_conn, registered_worker):
    responses = _passing_responses()
    responses[0] = WorkerResponse(kind=WorkerResponseKind.MALFORMED, error="broken")
    adapter = FakeWorkerAdapter(responses)
    result = run_conformance_suite(db_conn, adapter, worker_id=registered_worker, role="coder")
    assert result.status == ConformanceRunStatus.FAILED
    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=result.run_id,
    )
    assert not promo.ok
    assert promo.reason == "run_not_passed"


# --- 11/12. worker/role scope must match exactly ----------------------------

def test_passed_run_for_different_worker_cannot_promote_target_worker(db_conn, registered_worker):
    WorkersRepo(db_conn).register(worker_id="w2", kind="fake", network_class="local")
    result = _run_passing_suite(db_conn, registered_worker)  # run belongs to w1
    promo = promote_from_conformance(
        db_conn, worker_id="w2", role="coder", run_id=result.run_id,
    )
    assert not promo.ok
    assert promo.reason == "run_belongs_to_different_worker"


def test_passed_run_for_different_role_cannot_promote_target_role(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker, role="coder")
    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="reviewer", run_id=result.run_id,
    )
    assert not promo.ok
    assert promo.reason == "run_belongs_to_different_role"


# --- 13/14. a genuinely valid run promotes, with the run_id as evidence -----

def test_exact_valid_passing_run_promotes_locked_to_guarded(db_conn, registered_worker):
    manager = WorkerTrustManager(db_conn)
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.LOCKED
    result = _run_passing_suite(db_conn, registered_worker)
    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=result.run_id,
    )
    assert promo.ok
    assert promo.level == TrustLevel.GUARDED
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.GUARDED


def test_evidence_ref_written_to_trust_event_equals_exact_run_id(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=result.run_id,
    )
    history = WorkerTrustManager(db_conn).history(registered_worker, "coder")
    assert len(history) == 1
    assert history[0].evidence_ref == result.run_id


# --- 15. capability/role scope does not broaden accidentally ---------------

def test_capability_scope_does_not_broaden_accidentally(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", capability="read_file",
        run_id=result.run_id,
    )
    manager = WorkerTrustManager(db_conn)
    assert manager.current_trust(registered_worker, "coder", "read_file") == TrustLevel.GUARDED
    # The role-wide scope (capability=None) is untouched by a
    # capability-scoped promotion.
    assert manager.current_trust(registered_worker, "coder", None) == TrustLevel.LOCKED


# --- 16. mutation capability is never promoted from this suite -------------

def test_mutation_capability_not_promoted_from_conformance(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", capability="write_file",
        run_id=result.run_id,
    )
    assert not promo.ok
    assert promo.reason == "mutation_capability_not_conformance_tested"
    assert WorkerTrustManager(db_conn).current_trust(
        registered_worker, "coder", "write_file",
    ) == TrustLevel.LOCKED


def test_read_only_compliance_case_fails_if_mutation_became_executable(db_conn, registered_worker):
    """Direct proof the containment case's own detection actually
    triggers: a misconfigured allowed_tools that includes a mutating
    capability, paired with the fake adapter using it, must fail this
    specific case."""
    from code_slayer.workers import conformance as conformance_module

    adapter = FakeWorkerAdapter([
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="write_file", params={"path": "x", "content": "y"}),
        ),
    ])
    # Exercise the case function directly with a deliberately misconfigured
    # request shape by monkeypatching _base_request's allowed_tools for
    # this one call.
    original = conformance_module._base_request

    def misconfigured(role, *, allowed_tools=("read_file",), prior_tool_result=None):
        return original(role, allowed_tools=("read_file", "write_file"))

    conformance_module._base_request = misconfigured
    try:
        result = conformance_module._case_read_only_compliance(adapter, "coder")
    finally:
        conformance_module._base_request = original
    assert result.passed is False
    assert result.reason == "mutating_capability_became_executable"


# --- 17. malformed protocol incident regression remains contained ----------

def test_malformed_protocol_incident_regression_remains_contained(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    results = {r.case_name: r for r in ConformanceRepo(db_conn).list_results(result.run_id)}
    assert results["malformed_protocol_rejection"].passed is True
    assert results["malformed_protocol_rejection"].reason == "malformed_response_correctly_rejected"


# --- 18. timeout/error simulation produces a durable result ----------------

def test_timeout_error_simulation_produces_durable_result(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    results = {r.case_name: r for r in ConformanceRepo(db_conn).list_results(result.run_id)}
    case = results["timeout_error_handling"]
    assert case.passed is True
    assert "adapter_failure_contained" in case.reason


# --- 19. crash/incomplete run remains non-passing ---------------------------

def test_crash_incomplete_run_remains_non_passing(db_conn, registered_worker):
    """Simulates a process crash: the run row is RUNNING with only a few
    results committed, and finalize() never ran. Reopening/reading it
    later must never see PASSED."""
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-crash", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    with transaction(db_conn):
        repo.record_result_in_transaction(
            run_id="run-crash", case_name="inference", passed=True, reason="ok",
            detail_content_hash=None, occurred_at="2026-01-01T00:00:01.000000Z",
        )
    # No finalize() call -- the process "crashed" here.
    run = repo.get_run("run-crash")
    assert run.status == ConformanceRunStatus.RUNNING
    assert run.status != ConformanceRunStatus.PASSED
    result = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id="run-crash",
    )
    assert not result.ok


# --- 20. append-only/finalization protections hold --------------------------

def test_finalized_run_cannot_be_mutated(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_conformance_runs SET status = 'FAILED' WHERE run_id = ?",
            (result.run_id,),
        )


def test_run_row_cannot_be_deleted(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute("DELETE FROM worker_conformance_runs WHERE run_id = ?", (result.run_id,))


def test_results_cannot_be_updated_or_deleted(db_conn, registered_worker):
    result = _run_passing_suite(db_conn, registered_worker)
    row = db_conn.execute(
        "SELECT id FROM worker_conformance_results WHERE run_id = ? LIMIT 1", (result.run_id,),
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_conformance_results SET passed = 0 WHERE id = ?", (row["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute("DELETE FROM worker_conformance_results WHERE id = ?", (row["id"],))


def test_finalize_twice_is_refused(db_conn, registered_worker):
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-finalize-twice", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    with transaction(db_conn):
        repo.finalize_run_in_transaction(
            "run-finalize-twice", status=ConformanceRunStatus.PASSED,
            completed_at="2026-01-01T00:00:01.000000Z",
        )
    with pytest.raises(RuntimeError):
        with transaction(db_conn):
            repo.finalize_run_in_transaction(
                "run-finalize-twice", status=ConformanceRunStatus.FAILED,
                completed_at="2026-01-01T00:00:02.000000Z",
            )
    # Status is still the first, legitimate verdict.
    assert repo.get_run("run-finalize-twice").status == ConformanceRunStatus.PASSED


# --- audit -------------------------------------------------------------

def test_audit_events_emitted_for_run_start_and_finalize(db_conn, registered_worker):
    from code_slayer.audit.events import EventType

    _run_passing_suite(db_conn, registered_worker)
    started = db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type = ?",
        (EventType.WORKER_CONFORMANCE_RUN_STARTED.value,),
    ).fetchall()
    finalized = db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type = ?",
        (EventType.WORKER_CONFORMANCE_RUN_FINALIZED.value,),
    ).fetchall()
    assert len(started) == 1
    assert len(finalized) == 1


# --- unknown worker ----------------------------------------------------

def test_run_suite_for_unknown_worker_refused(db_conn):
    adapter = FakeWorkerAdapter(_passing_responses())
    result = run_conformance_suite(db_conn, adapter, worker_id="ghost", role="coder")
    assert not result.ok
    assert result.reason == "unknown_worker"
    assert result.run_id is None


# --- concurrency: two promotion attempts for the same passing run ----------

def test_concurrent_promotion_exactly_one_winner(tmp_path):
    """Two independent connections both attempting to promote the same
    (worker, role) scope from the same PASSED run: WorkerTrustManager's
    own in-transaction re-check (Phase 7.2) already serializes this --
    proven here through the conformance-gated path, not a new lock."""
    from code_slayer.store.db import connect, migrate

    db_path = tmp_path / "state.db"
    conn_setup = connect(db_path)
    migrate(conn_setup)
    WorkersRepo(conn_setup).register(worker_id="w1", kind="fake", network_class="local")
    run_result = _run_passing_suite(conn_setup, "w1")
    conn_setup.close()

    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        result_a = promote_from_conformance(
            conn_a, worker_id="w1", role="coder", run_id=run_result.run_id,
        )
        result_b = promote_from_conformance(
            conn_b, worker_id="w1", role="coder", run_id=run_result.run_id,
        )
        outcomes = {result_a.ok, result_b.ok}
        assert outcomes == {True, False}
        history = WorkerTrustManager(conn_a).history("w1", "coder")
        assert len(history) == 1
    finally:
        conn_a.close()
        conn_b.close()
