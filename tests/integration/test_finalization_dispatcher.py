"""`finalization.dispatcher.TaskLifecycleExecutor`: the server-owned
background dispatcher that automatically rediscovers and resumes tasks
stranded in `READY_FOR_CHECKPOINT`/`CHECKPOINTED`, and interrupted
`checkpoint_create` operations, after a process/runtime restart --
against real temporary Git repos and real SQLite state databases.

Covers: startup discovery-and-resume for both stranded states; a real
Git-ref-exists-but-DB-interrupted `checkpoint_create` reconciling
automatically through the dispatcher; active-lease skip and later
recovery once released; two dispatcher instances racing on the same task
resolving to exactly one checkpoint/one completion; idempotent repeated
polling after COMPLETED; a state change between discovery and action
never causing a stale write; recovery across two full executor restarts;
that no test here ever calls `LocalWorkerRunner.resume()`/
`ApplicationService.resume()`; that `bounded_read_only_turn` runs are
invisible to the dispatcher; that the model mutation tool offering is
unaffected; and one full `ApplicationService`-level proof that recovery
is server-owned, requiring no client/HTTP resume call at all.
"""

from __future__ import annotations

import ast
import pathlib
import threading
import time

from code_slayer.api.service import ApplicationService, RuntimeBindings
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.finalization import dispatcher as dispatcher_module
from code_slayer.finalization.dispatcher import TaskLifecycleExecutor
from code_slayer.finalization.lifecycle import (
    CheckpointAdvanceOutcome,
    advance_ready_for_checkpoint,
)
from code_slayer.finalization.service import Finalizer
from code_slayer.finalization.types import FinalizerVerdict
from code_slayer.lease.manager import LeaseManager
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.checkpoint import CheckpointManager
from code_slayer.runner import LocalWorkerRunner, RunStatus
from code_slayer.store import location
from code_slayer.store.checkpoint_repo import CheckpointRepo
from code_slayer.store.db import transaction
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest
from code_slayer.workers.conformance import run_conformance_suite
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.fake_prompt_analyst import FakePromptAnalyst
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.prompt_analysis import PromptAnalysis
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from tests.repo_helpers import acquire_lease, commit

# -- shared helpers -----------------------------------------------------


def _advance(machine, task_id, *pairs):
    for expected, to_state in pairs:
        machine.transition(task_id, expected_state=expected, to_state=to_state, reason="progress")


def _new_task(conn, root, blobs_dir):
    (root / "pyproject.toml").write_text("[tool.ruff]\n")
    commit(root, "add verification config")
    info = identity.resolve(root)
    task = TaskRepo(conn).create(
        description="dispatcher flow", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(conn, blobs_dir=blobs_dir)
    service.start(task.task_id)
    service.capture(task.task_id)
    return task


def _reach_ready_for_checkpoint(conn, task_id, blobs_dir, lease, tmp_dir):
    executor = ToolExecutor(conn, blobs_dir=blobs_dir, lease=lease)
    result = executor.execute(
        task_id, ToolRequest(tool="create_file", path="clean.py", content=b"VALUE = 1\n"),
    )
    assert result.status == OperationStatus.SUCCEEDED
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.VERIFYING, reason="p",
    )
    decision = Finalizer(conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir).decide_after_verification(
        task_id, lease,
    )
    assert decision.verdict == FinalizerVerdict.VERIFIED
    assert TaskRepo(conn).get(task_id).state == "READY_FOR_CHECKPOINT"


def _plant_ready_for_checkpoint(root):
    """Build a real task, real git content, real verification, ending at
    READY_FOR_CHECKPOINT -- entirely through a `LocalWorkerRunner`'s
    control-plane connection, which is closed again before returning, so
    the only thing left "live" is durable state a `TaskLifecycleExecutor`
    resolving the same repository will independently find."""
    runner = LocalWorkerRunner(root)
    try:
        conn = runner._control_conn
        blobs_dir = runner._control_blobs_dir
        tmp_dir = location.tmp_dir(runner._primary.repo_id, runner._primary.worktree_id)
        task = _new_task(conn, root, blobs_dir)
        machine = TaskStateMachine(conn)
        _advance(machine, task.task_id,
                  (TaskState.BASELINED, TaskState.PLANNING),
                  (TaskState.PLANNING, TaskState.PLANNED),
                  (TaskState.PLANNED, TaskState.IMPLEMENTING))
        lease = acquire_lease(conn, task, worker_id="planting-worker")
        _reach_ready_for_checkpoint(conn, task.task_id, blobs_dir, lease, tmp_dir)
        LeaseManager(conn).release(lease)
        return task.task_id
    finally:
        runner.close()


def _plant_checkpointed(root):
    """As `_plant_ready_for_checkpoint()`, additionally driven all the
    way to a real CHECKPOINTED (never COMPLETED) before any dispatcher
    ever runs."""
    runner = LocalWorkerRunner(root)
    try:
        conn = runner._control_conn
        blobs_dir = runner._control_blobs_dir
        tmp_dir = location.tmp_dir(runner._primary.repo_id, runner._primary.worktree_id)
        task = _new_task(conn, root, blobs_dir)
        machine = TaskStateMachine(conn)
        _advance(machine, task.task_id,
                  (TaskState.BASELINED, TaskState.PLANNING),
                  (TaskState.PLANNING, TaskState.PLANNED),
                  (TaskState.PLANNED, TaskState.IMPLEMENTING))
        lease = acquire_lease(conn, task, worker_id="planting-worker")
        _reach_ready_for_checkpoint(conn, task.task_id, blobs_dir, lease, tmp_dir)
        result = advance_ready_for_checkpoint(
            conn, task.task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
        )
        assert result.outcome == CheckpointAdvanceOutcome.CREATED
        LeaseManager(conn).release(lease)
        return task.task_id
    finally:
        runner.close()


def _task_state(root, task_id):
    runner = LocalWorkerRunner(root)
    try:
        return TaskRepo(runner._control_conn).get(task_id).state
    finally:
        runner.close()


def _checkpoint_count(root, task_id):
    runner = LocalWorkerRunner(root)
    try:
        return len(CheckpointRepo(runner._control_conn).list_for_task(task_id))
    finally:
        runner.close()


def _wait_until(predicate, *, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def _read_call(path="README.md"):
    return WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL,
        tool_call=WorkerToolCall(tool="read_file", params={"path": path}),
    )


def _text(content="ok"):
    return WorkerResponse(kind=WorkerResponseKind.TEXT, text=content)


def _grant_read_trust(conn, *, worker_id="w", role="coder"):
    """Real, durable trust earned through the actual conformance/
    promotion APIs -- mirrors `test_local_worker_runner.py`'s own
    `_grant_guarded_via_conformance()` -- never a manually inserted
    trust row."""
    responses = [
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="hi there"),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="clean output"),
        _read_call(), WorkerResponse(kind=WorkerResponseKind.TEXT, text="continuing"), _read_call(),
    ]
    adapter = FakeWorkerAdapter(responses)
    suite = run_conformance_suite(conn, adapter, worker_id=worker_id, role=role)
    assert suite.ok and suite.status == "PASSED", suite
    result = promote_from_conformance(
        conn, worker_id=worker_id, role=role, capability="read_file", run_id=suite.run_id,
    )
    assert result.ok, result.reason


# --- A. startup discovers READY_FOR_CHECKPOINT -------------------------

def test_a_startup_discovers_ready_for_checkpoint_and_completes_automatically(
    git_repo_with_commit,
):
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)

    executor = TaskLifecycleExecutor(root)
    try:
        executor.start()  # one immediate discovery-and-resume pass, synchronously
        assert _task_state(root, task_id) == "COMPLETED"
        assert _checkpoint_count(root, task_id) == 1
    finally:
        executor.stop()


# --- B. startup discovers CHECKPOINTED ----------------------------------

def test_b_startup_discovers_checkpointed_and_completes_automatically(git_repo_with_commit):
    root = git_repo_with_commit
    task_id = _plant_checkpointed(root)
    assert _task_state(root, task_id) == "CHECKPOINTED"

    executor = TaskLifecycleExecutor(root)
    try:
        executor.start()
        assert _task_state(root, task_id) == "COMPLETED"
        assert _checkpoint_count(root, task_id) == 1
    finally:
        executor.stop()


# --- C. stranded checkpoint_create (real Git ref, interrupted DB) ------

def test_c_stranded_checkpoint_create_reconciles_via_dispatcher(git_repo_with_commit, monkeypatch):
    """A real checkpoint Git ref is created, but the durable
    finish/checkpoints-row/state-transition step never runs (simulated
    crash) -- exactly Part 5's required scenario. The dispatcher's
    startup pass must discover the stranded `checkpoint_create` via the
    existing `lease.recovery`/`CheckpointManager.reconcile()` path and
    let the task progress correctly, all the way to COMPLETED, in one
    call."""
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)

    runner = LocalWorkerRunner(root)
    try:
        conn = runner._control_conn
        blobs_dir = runner._control_blobs_dir
        tmp_dir = location.tmp_dir(runner._primary.repo_id, runner._primary.worktree_id)
        task = TaskRepo(conn).get(task_id)
        lease = acquire_lease(conn, task, worker_id="crash-simulator")

        real_finalize = CheckpointManager._finalize
        calls = {"n": 0}

        def crash_once_then_real(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash after git ref creation")
            return real_finalize(self, **kwargs)

        monkeypatch.setattr(CheckpointManager, "_finalize", crash_once_then_real)

        manager = CheckpointManager(conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir, lease=lease)
        result = manager.create(task_id)
        assert result.operation_status == OperationStatus.UNKNOWN
        assert TaskRepo(conn).get(task_id).state == "READY_FOR_CHECKPOINT"
        unresolved = conn.execute(
            "SELECT 1 FROM tool_operations WHERE task_id = ? "
            "AND status IN ('STARTED', 'UNKNOWN')",
            (task_id,),
        ).fetchall()
        assert len(unresolved) == 1
        LeaseManager(conn).release(lease)
    finally:
        runner.close()

    executor = TaskLifecycleExecutor(root)
    try:
        executor.start()
        assert _task_state(root, task_id) == "COMPLETED"
        assert _checkpoint_count(root, task_id) == 1
    finally:
        executor.stop()


# --- D. active lease -> dispatcher skips --------------------------------

def test_d_active_lease_causes_dispatcher_to_skip(git_repo_with_commit):
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)

    runner = LocalWorkerRunner(root)
    try:
        conn = runner._control_conn
        task = TaskRepo(conn).get(task_id)
        held = LeaseManager(conn).acquire(
            worktree_id=task.worktree_id, task_id=task_id,
            worker_id="someone-else", worker_session_id="s1",
        )
        assert held.handle is not None
    finally:
        runner.close()

    executor = TaskLifecycleExecutor(root)
    try:
        executor.start()
        assert _task_state(root, task_id) == "READY_FOR_CHECKPOINT"
        assert _checkpoint_count(root, task_id) == 0
    finally:
        executor.stop()


# --- E. lease released later -> later poll completes automatically -----

def test_e_lease_release_allows_later_poll_to_complete(git_repo_with_commit):
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)

    runner = LocalWorkerRunner(root)
    try:
        conn = runner._control_conn
        task = TaskRepo(conn).get(task_id)
        held = LeaseManager(conn).acquire(
            worktree_id=task.worktree_id, task_id=task_id,
            worker_id="someone-else", worker_session_id="s1",
        )
        held_handle = held.handle
        assert held_handle is not None
    finally:
        runner.close()

    executor = TaskLifecycleExecutor(root, poll_interval_seconds=0.02)
    try:
        executor.start()
        assert _task_state(root, task_id) == "READY_FOR_CHECKPOINT"  # first tick: skipped

        runner2 = LocalWorkerRunner(root)
        try:
            LeaseManager(runner2._control_conn).release(held_handle)
        finally:
            runner2.close()

        executor.notify()
        _wait_until(lambda: _task_state(root, task_id) == "COMPLETED")
    finally:
        executor.stop()


# --- F. two executors contend for the same task -------------------------

def test_f_two_executors_contend_only_one_wins(git_repo_with_commit):
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)

    executor_a = TaskLifecycleExecutor(root)
    executor_b = TaskLifecycleExecutor(root)
    barrier = threading.Barrier(2)

    def run(executor):
        barrier.wait()
        executor._dispatch_once()

    thread_a = threading.Thread(target=run, args=(executor_a,))
    thread_b = threading.Thread(target=run, args=(executor_b,))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert _task_state(root, task_id) == "COMPLETED"
    assert _checkpoint_count(root, task_id) == 1


# --- G. idempotent polling after COMPLETED ------------------------------

def test_g_idempotent_polling_after_completed_creates_no_new_side_effects(git_repo_with_commit):
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)
    executor = TaskLifecycleExecutor(root)
    executor.start()
    assert _task_state(root, task_id) == "COMPLETED"

    def _audit_count():
        runner = LocalWorkerRunner(root)
        try:
            return runner._control_conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE task_id = ?", (task_id,),
            ).fetchone()["c"]
        finally:
            runner.close()

    events_before = _audit_count()
    checkpoints_before = _checkpoint_count(root, task_id)

    executor._dispatch_once()
    executor._dispatch_once()
    executor.stop()

    assert _checkpoint_count(root, task_id) == checkpoints_before == 1
    assert _audit_count() == events_before
    assert _task_state(root, task_id) == "COMPLETED"


# --- H. state changed between discovery and action ----------------------

def test_h_state_changed_after_discovery_prevents_stale_action(git_repo_with_commit, monkeypatch):
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)
    executor = TaskLifecycleExecutor(root)
    real_discover = dispatcher_module.discover_ready_for_checkpoint

    def discover_then_race(conn):
        found = real_discover(conn)
        # Another actor (simulated) forces the SAME task straight to
        # CHECKPOINTED, with no real checkpoint underneath it, between
        # this discovery read and the dispatcher's own subsequent
        # lease-acquire-and-act step -- this repository's own
        # established adversarial-content test technique.
        with transaction(conn):
            TaskRepo(conn)._record_transition_in_transaction(
                task_id, to_state="CHECKPOINTED", to_phase="CHECKPOINTED", reason="simulated race",
            )
        return found

    monkeypatch.setattr(dispatcher_module, "discover_ready_for_checkpoint", discover_then_race)

    executor.start()
    executor.stop()

    # Never a duplicate/stale checkpoint attempt on top of the forced
    # state, and never a faked completion for a CHECKPOINTED task with no
    # real checkpoint evidence.
    assert _task_state(root, task_id) == "CHECKPOINTED"
    assert _checkpoint_count(root, task_id) == 0


# --- I. restart twice recovers from an intermediate state --------------

def test_i_restart_twice_recovers_from_intermediate_state(git_repo_with_commit, monkeypatch):
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)

    real_advance_checkpointed_completion = dispatcher_module.advance_checkpointed_completion

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash before completion")

    monkeypatch.setattr(dispatcher_module, "advance_checkpointed_completion", boom)

    executor1 = TaskLifecycleExecutor(root)
    executor1.start()
    executor1.stop()
    assert _task_state(root, task_id) == "CHECKPOINTED"
    assert _checkpoint_count(root, task_id) == 1

    # Restore only this one attribute -- `monkeypatch.undo()` would also
    # revert the autouse `CODESLAYER_STATE_ROOT` env-var patch this same
    # (function-scoped, shared) `monkeypatch` fixture instance carries,
    # which would point a freshly constructed executor at an entirely
    # different state root.
    monkeypatch.setattr(
        dispatcher_module, "advance_checkpointed_completion", real_advance_checkpointed_completion,
    )

    executor2 = TaskLifecycleExecutor(root)
    executor2.start()
    executor2.stop()
    assert _task_state(root, task_id) == "COMPLETED"
    assert _checkpoint_count(root, task_id) == 1


# --- J. no runner resume used anywhere in this file ---------------------

def test_j_no_runner_resume_used_anywhere_in_this_test_file():
    """Meta-test: statically proves nothing in this file calls
    `LocalWorkerRunner.resume()` / `ApplicationService.resume()` -- every
    recovery proven here is dispatcher-only, never client/HTTP
    resume-triggered."""
    tree = ast.parse(pathlib.Path(__file__).read_text())
    resume_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "resume"
    ]
    assert resume_calls == []


# --- K. bounded_read_only_turn unaffected -------------------------------

def test_k_bounded_read_only_turn_unaffected_by_dispatcher(git_repo_with_commit):
    root = git_repo_with_commit
    runner = LocalWorkerRunner(root)
    try:
        WorkersRepo(runner._control_conn).register(
            worker_id="w", kind="fake", network_class="local",
        )
        _grant_read_trust(runner._control_conn)
        prompt = "Read the README.md file."
        analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
        result = runner.start(
            original_prompt=prompt, worker_id="w", role="coder", prompt_analyst=analyst,
            adapter=FakeWorkerAdapter([_read_call(), _text()]),
        )
        assert result.status == RunStatus.COMPLETED
        row = RunnerRepo(runner._control_conn).get(result.run_id)
        assert TaskRepo(runner._control_conn).get(row.task_id).state == "COMPLETED"
    finally:
        runner.close()

    executor = TaskLifecycleExecutor(root)
    executor.start()
    snapshot = executor.snapshot()
    executor.stop()
    assert snapshot.last_ready_for_checkpoint_discovered == 0
    assert snapshot.last_checkpointed_discovered == 0


# --- L. mutation tool offering unchanged ---------------------------------

def test_l_mutation_tool_offering_unchanged():
    from code_slayer.workers.execution import _ALLOWED_TOOLS

    assert _ALLOWED_TOOLS == ("read_file",)


# --- Part 11: real ApplicationService lifecycle, no resume API ---------

def test_application_service_recovers_stranded_task_with_no_resume_api_call(git_repo_with_commit):
    """The production integration point: a task stranded in
    READY_FOR_CHECKPOINT, an `ApplicationService` constructed exactly as
    `cli.main.serve` constructs one, no `resume()` call anywhere, and the
    task still reaches COMPLETED automatically -- proving a real
    production caller exists, not merely the executor class in
    isolation."""
    root = git_repo_with_commit
    task_id = _plant_ready_for_checkpoint(root)

    service = ApplicationService(
        root, bindings=RuntimeBindings(lifecycle_poll_interval_seconds=0.02),
    )
    try:
        _wait_until(lambda: _task_state(root, task_id) == "COMPLETED")
    finally:
        service.close()

    assert _checkpoint_count(root, task_id) == 1


def test_application_service_discovers_stranded_checkpointed_task(git_repo_with_commit):
    root = git_repo_with_commit
    task_id = _plant_checkpointed(root)

    service = ApplicationService(
        root, bindings=RuntimeBindings(lifecycle_poll_interval_seconds=0.02),
    )
    try:
        _wait_until(lambda: _task_state(root, task_id) == "COMPLETED")
    finally:
        service.close()
    assert _checkpoint_count(root, task_id) == 1
