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
from code_slayer.workers.protocol import (
    WorkerAdapterError,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)
from code_slayer.workers.trust import WorkerTrustManager


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


class _FakeClock:
    """A controllable clock, matching the pattern already used in
    `tests/unit/test_lease_manager.py`, so run/downgrade ordering can be
    tested exactly, including the equal-timestamp edge case."""

    def __init__(self, start: str = "2026-01-01T00:00:00.000000Z") -> None:
        self.now = start

    def __call__(self) -> str:
        return self.now


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
        WorkerAdapterError("simulated_timeout"),  # timeout_error_handling -- expected shape
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
    """Direct proof this is genuine WORKER evidence, not a containment
    check: a misconfigured allowed_tools that includes a mutating
    capability, paired with a fake adapter that actually uses it, must
    fail this case because the *worker* requested a mutation — nothing
    here depends on whether Code Slayer would separately have blocked
    it (it would have, via validate_response's own allowlist check, but
    that is not what this case is grading)."""
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
    assert result.reason == "worker_requested_mutating_capability"


def test_read_only_compliance_fails_on_unauthorized_capability_even_though_contained(
    db_conn, registered_worker,
):
    """Finding 1's central requirement, tested directly: the worker asks
    for a capability outside its offered scope; validate_response()
    correctly denies it as UNAUTHORIZED_CAPABILITY (containment holds,
    nothing executes) -- but the case must still FAIL, because
    containment succeeding is not evidence the worker behaved well."""
    from code_slayer.workers import conformance as conformance_module
    from code_slayer.workers.protocol_validation import ValidationOutcome, validate_response

    # First, confirm containment itself genuinely holds for this response
    # shape (the premise the case's own FAIL must not be confused with).
    probe_adapter = FakeWorkerAdapter([
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="run_command", params={}),
        ),
    ])
    request = conformance_module._base_request("coder", allowed_tools=("read_file",))
    outcome = validate_response(request, probe_adapter.infer(request))
    assert outcome.outcome == ValidationOutcome.UNAUTHORIZED_CAPABILITY
    assert not outcome.executable  # containment held

    # Now confirm the actual case, given the identical response shape,
    # still FAILs -- containment holding did not convert it to a PASS.
    case_adapter = FakeWorkerAdapter([
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="run_command", params={}),
        ),
    ])
    case_result = conformance_module._case_read_only_compliance(case_adapter, "coder")
    assert case_result.passed is False
    assert case_result.reason == "worker_requested_unauthorized_capability"


def test_containment_case_passes_independently_of_worker_evidence_case_failing(
    db_conn, registered_worker,
):
    """malformed_protocol_rejection (containment) must still PASS in a
    run where read_only_compliance (worker evidence) FAILs -- neither
    case's outcome is derived from the other's."""
    responses = _passing_responses()
    # Break read_only_compliance: worker requests a mutating capability.
    responses[4] = WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL,
        tool_call=WorkerToolCall(tool="write_file", params={"path": "x", "content": "y"}),
    )
    adapter = FakeWorkerAdapter(responses)
    result = run_conformance_suite(db_conn, adapter, worker_id=registered_worker, role="coder")
    results = {r.case_name: r.passed for r in ConformanceRepo(db_conn).list_results(result.run_id)}
    assert results["read_only_compliance"] is False
    assert results["malformed_protocol_rejection"] is True
    # Still fails overall -- a required case is missing.
    assert result.status == ConformanceRunStatus.FAILED


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
    assert "expected_adapter_failure_contained" in case.reason


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


# --- staleness: a run must not outlive a later downgrade -------------------

def test_downgrade_makes_the_already_used_run_stale(db_conn, registered_worker):
    clock = _FakeClock("2026-01-01T00:00:00.000000Z")
    adapter = FakeWorkerAdapter(_passing_responses())
    run_a = run_conformance_suite(
        db_conn, adapter, worker_id=registered_worker, role="coder", now_fn=clock,
    )
    assert run_a.status == ConformanceRunStatus.PASSED

    first_promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_a.run_id,
    )
    assert first_promo.ok

    clock.now = "2026-01-01T01:00:00.000000Z"
    manager = WorkerTrustManager(db_conn, now_fn=clock)
    downgrade = manager.downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="malformed_tool_call",
    )
    assert downgrade.ok
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.LOCKED

    # The exact same run_id, already used once, cannot re-promote after
    # the downgrade.
    second_promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_a.run_id,
    )
    assert not second_promo.ok
    assert second_promo.reason == "run_stale_relative_to_latest_trust_event"
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.LOCKED


def test_a_different_run_completed_before_downgrade_is_also_stale(db_conn, registered_worker):
    clock = _FakeClock("2026-01-01T00:00:00.000000Z")

    run_a = run_conformance_suite(
        db_conn, FakeWorkerAdapter(_passing_responses()),
        worker_id=registered_worker, role="coder", now_fn=clock,
    )
    promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_a.run_id,
    )

    # A second, independent passing run, completed BEFORE the downgrade
    # below -- never used for the first promotion, but still too old.
    clock.now = "2026-01-01T00:30:00.000000Z"
    run_b = run_conformance_suite(
        db_conn, FakeWorkerAdapter(_passing_responses()),
        worker_id=registered_worker, role="coder", now_fn=clock,
    )
    assert run_b.status == ConformanceRunStatus.PASSED

    clock.now = "2026-01-01T01:00:00.000000Z"
    WorkerTrustManager(db_conn, now_fn=clock).downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="malformed_tool_call",
    )

    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_b.run_id,
    )
    assert not promo.ok
    assert promo.reason == "run_stale_relative_to_latest_trust_event"


def test_fresh_run_started_after_downgrade_can_promote(db_conn, registered_worker):
    clock = _FakeClock("2026-01-01T00:00:00.000000Z")
    run_a = run_conformance_suite(
        db_conn, FakeWorkerAdapter(_passing_responses()),
        worker_id=registered_worker, role="coder", now_fn=clock,
    )
    promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_a.run_id,
    )

    clock.now = "2026-01-01T01:00:00.000000Z"
    WorkerTrustManager(db_conn, now_fn=clock).downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="malformed_tool_call",
    )

    clock.now = "2026-01-01T02:00:00.000000Z"
    run_c = run_conformance_suite(
        db_conn, FakeWorkerAdapter(_passing_responses()),
        worker_id=registered_worker, role="coder", now_fn=clock,
    )
    assert run_c.status == ConformanceRunStatus.PASSED

    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_c.run_id,
    )
    assert promo.ok
    assert promo.level == TrustLevel.GUARDED


def test_equal_freshness_boundary_fails_closed(db_conn, registered_worker):
    """A run whose started_at exactly equals the latest trust event's
    occurred_at must not be treated as fresh enough -- strict `>` only."""
    clock = _FakeClock("2026-01-01T00:00:00.000000Z")
    run_a = run_conformance_suite(
        db_conn, FakeWorkerAdapter(_passing_responses()),
        worker_id=registered_worker, role="coder", now_fn=clock,
    )
    promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_a.run_id,
    )

    downgrade_time = "2026-01-01T01:00:00.000000Z"
    clock.now = downgrade_time
    WorkerTrustManager(db_conn, now_fn=clock).downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="malformed_tool_call",
    )

    # run_d's started_at is set to EXACTLY the downgrade's own timestamp.
    clock.now = downgrade_time
    run_d = run_conformance_suite(
        db_conn, FakeWorkerAdapter(_passing_responses()),
        worker_id=registered_worker, role="coder", now_fn=clock,
    )
    assert run_d.status == ConformanceRunStatus.PASSED

    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run_d.run_id,
    )
    assert not promo.ok
    assert promo.reason == "run_stale_relative_to_latest_trust_event"


def test_no_prior_trust_history_means_no_freshness_constraint(db_conn, registered_worker):
    """A worker/role/capability scope with no trust history at all has
    nothing to be stale relative to -- any passing run, regardless of
    when it started, may promote it."""
    run = run_conformance_suite(
        db_conn, FakeWorkerAdapter(_passing_responses()),
        worker_id=registered_worker, role="coder",
    )
    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=run.run_id,
    )
    assert promo.ok


# --- timeout/error: only the expected failure shape counts -----------------

def test_unexpected_exception_does_not_count_as_timeout_containment(db_conn, registered_worker):
    responses = _passing_responses()
    responses[6] = KeyError("unexpected programming error, not a timeout")
    adapter = FakeWorkerAdapter(responses)
    result = run_conformance_suite(db_conn, adapter, worker_id=registered_worker, role="coder")
    results = {r.case_name: r for r in ConformanceRepo(db_conn).list_results(result.run_id)}
    case = results["timeout_error_handling"]
    assert case.passed is False
    assert "unexpected_exception_not_valid_containment" in case.reason
    assert "KeyError" in case.reason
    assert result.status == ConformanceRunStatus.FAILED


def test_unexpected_adapter_exception_prevents_passing_promotion(db_conn, registered_worker):
    responses = _passing_responses()
    responses[6] = ValueError("also not a valid timeout shape")
    adapter = FakeWorkerAdapter(responses)
    result = run_conformance_suite(db_conn, adapter, worker_id=registered_worker, role="coder")
    assert result.status == ConformanceRunStatus.FAILED
    promo = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", run_id=result.run_id,
    )
    assert not promo.ok
    assert promo.reason == "run_not_passed"


# --- run identity immutability ----------------------------------------------

def test_run_identity_fields_cannot_be_changed_while_running(db_conn, registered_worker):
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-identity", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_conformance_runs SET role = 'reviewer' WHERE run_id = 'run-identity'",
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_conformance_runs SET worker_id = 'someone-else' "
            "WHERE run_id = 'run-identity'",
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_conformance_runs SET started_at = '2099-01-01T00:00:00.000000Z' "
            "WHERE run_id = 'run-identity'",
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_conformance_runs SET suite_version = 'other-suite' "
            "WHERE run_id = 'run-identity'",
        )
    # Still exactly as created -- none of the attempts above took effect.
    run = repo.get_run("run-identity")
    assert run.role == "coder"
    assert run.worker_id == registered_worker
    assert run.started_at == "2026-01-01T00:00:00.000000Z"
    assert run.suite_version == SUITE_VERSION


def test_legitimate_finalize_transition_still_works_with_identity_lock(db_conn, registered_worker):
    repo = ConformanceRepo(db_conn)
    with transaction(db_conn):
        repo.start_run_in_transaction(
            run_id="run-finalize-ok", worker_id=registered_worker, role="coder",
            suite_version=SUITE_VERSION, started_at="2026-01-01T00:00:00.000000Z",
        )
    with transaction(db_conn):
        run = repo.finalize_run_in_transaction(
            "run-finalize-ok", status=ConformanceRunStatus.PASSED,
            completed_at="2026-01-01T00:00:01.000000Z",
        )
    assert run.status == ConformanceRunStatus.PASSED
    assert run.completed_at == "2026-01-01T00:00:01.000000Z"
    # Identity fields untouched by the legitimate finalize.
    assert run.worker_id == registered_worker
    assert run.role == "coder"
    assert run.started_at == "2026-01-01T00:00:00.000000Z"


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
