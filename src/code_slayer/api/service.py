"""Application wiring and safe read models. Only the runner performs actions.

Factories are trusted host configuration, never HTTP inputs. Each request owns
its connections and adapters; no SQLite connection crosses a server thread.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from code_slayer.api.reads import ReadModels
from code_slayer.finalization.dispatcher import TaskLifecycleExecutor
from code_slayer.intelligence import RepositoryIntelligenceService
from code_slayer.permissions.service import PermissionService
from code_slayer.planning.executor import PlanningJobExecutor
from code_slayer.planning.service import EngineeringPlanningService
from code_slayer.repo import identity
from code_slayer.runner import LocalWorkerRunner
from code_slayer.workers.prompt_analysis import EvidenceSource, PromptAnalyst
from code_slayer.workers.protocol import WorkerAdapter
from code_slayer.workers.question_gate import ResolutionKind


class APIError(Exception):
    def __init__(self, code, message, status=400, retryable=False):
        self.code, self.message = code, message
        self.status, self.retryable = status, retryable


@dataclass(frozen=True)
class WorkerRegistration:
    """One worker this runtime declares at server startup — the exact
    `(worker_id, kind, network_class)` `store.workers_repo.WorkersRepo.
    register()` needs. Declaring a worker here grants it no trust and no
    qualification/certification of any kind: it only makes the worker
    row exist so `ApplicationService.start()`'s existing `unknown_worker`
    check (`reads.worker(worker_id) is None`) can pass, and so `workers.
    trust.WorkerTrustManager`/`workers.cloud_escalation` have a real row
    to read `network_class` from. Every actual capability/role
    certification still comes entirely from conformance-gated trust
    promotion (`workers.promotion.promote_from_conformance`), never from
    this declaration."""

    worker_id: str
    kind: str
    network_class: str


@dataclass(frozen=True)
class RuntimeBindings:
    """Server-owned factories; registration and trust remain existing backend work."""

    analyst_factory: Callable[[], PromptAnalyst] | None = None
    adapter_factory: Callable[[str, str], WorkerAdapter | None] | None = None
    planner_factory: Callable[[], object] | None = None
    # Workers this runtime declares as existing (Phase: runtime
    # integration). Registered idempotently once at `ApplicationService`
    # construction -- never re-registered per request, and never itself
    # a trust/qualification grant; see `WorkerRegistration`.
    worker_registrations: tuple[WorkerRegistration, ...] = ()
    # Background planning executor tuning (Phase 8.2d) -- server
    # configuration only, never client-facing. Correctness over
    # throughput: one worker by default (see `planning.executor`'s
    # module docstring). A smaller poll interval is useful for tests;
    # production has no reason to lower it below the default.
    planning_max_workers: int = 1
    planning_poll_interval_seconds: float = 2.0
    # Restart/resume recovery dispatcher tuning (`finalization.dispatcher.
    # TaskLifecycleExecutor`) -- server configuration only, never
    # client-facing. A smaller poll interval is useful for tests;
    # production has no reason to lower it below the default.
    lifecycle_poll_interval_seconds: float = 2.0


class ApplicationService:
    def __init__(self, repo_path, *, state_root=None, bindings=None):
        self.repo_path = Path(repo_path).resolve()
        self.state_root = state_root
        self.bindings = bindings or RuntimeBindings()
        # Initialize through the real application service, including existing
        # migrations, and idempotently register every runtime-declared
        # worker (never a trust/qualification grant — see
        # `WorkerRegistration`) before anything else can reference it.
        with self.runner() as runner:
            for registration in self.bindings.worker_registrations:
                runner.register_worker(
                    worker_id=registration.worker_id, kind=registration.kind,
                    network_class=registration.network_class,
                )
        self.identity = identity.resolve(self.repo_path, create=False)
        # One long-lived background planning executor per process (Phase
        # 8.2d) -- never one per HTTP request. Only constructed/started
        # when a Planner is actually configured; when it is not, POST
        # /api/plans fails explicitly (see `_require_planner_configured()`)
        # rather than accepting a job nothing could ever execute.
        self._planning_executor = None
        if self.bindings.planner_factory is not None:
            self._planning_executor = PlanningJobExecutor(
                self.repo_path, planner_factory=self.bindings.planner_factory,
                state_root_override=self.state_root,
                max_workers=self.bindings.planning_max_workers,
                poll_interval_seconds=self.bindings.planning_poll_interval_seconds,
            )
            self._planning_executor.start()
        # One long-lived restart/resume recovery dispatcher per process,
        # started unconditionally -- unlike planning, it depends on no
        # external factory and no client ever needs to configure it in
        # (`finalization.dispatcher.TaskLifecycleExecutor`, closing the
        # orchestration gap the restart/resume audit found: nothing
        # previously re-discovered a task stranded in
        # READY_FOR_CHECKPOINT/CHECKPOINTED after a process restart).
        self._lifecycle_executor = TaskLifecycleExecutor(
            self.repo_path, state_root_override=self.state_root,
            poll_interval_seconds=self.bindings.lifecycle_poll_interval_seconds,
        )
        self._lifecycle_executor.start()

    def close(self) -> None:
        """Stop this instance's background dispatchers, if started.
        Production (`cli.main.serve`, run via `waitress`, itself run
        under the existing systemd-hosted process) has no call site for
        this -- each dispatcher's thread is a daemon thread and simply
        exits with the process, which is exactly Case B's own crash-safe
        recovery path exercised the ordinary way. This exists so tests
        (and any embedding context that constructs many short-lived
        `ApplicationService` instances in one process) can avoid leaking
        an unbounded number of live background dispatcher threads across
        a long-running test session."""
        if self._planning_executor is not None:
            self._planning_executor.stop()
        self._lifecycle_executor.stop()

    @contextmanager
    def runner(self):
        runner = LocalWorkerRunner(self.repo_path, state_root_override=self.state_root)
        try:
            yield runner
        finally:
            runner.close()

    @contextmanager
    def reads(self):
        with ReadModels(self.identity, self.state_root) as reads:
            yield reads

    @contextmanager
    def intelligence(self):
        service = RepositoryIntelligenceService(self.repo_path, state_root_override=self.state_root)
        try:
            yield service
        finally:
            service.close()

    @contextmanager
    def planning(self):
        service = EngineeringPlanningService(self.repo_path, state_root_override=self.state_root)
        try:
            yield service
        finally:
            service.close()

    @contextmanager
    def permissions(self):
        service = PermissionService(self.repo_path, state_root_override=self.state_root)
        try:
            yield service
        finally:
            service.close()

    def adapter(self, worker_id, role):
        factory = self.bindings.adapter_factory
        return factory(worker_id, role) if factory else None

    def start(self, data):
        with self.reads() as reads:
            if reads.worker(data["worker_id"]) is None:
                raise APIError("unknown_worker", "Worker is not registered.", 404)
        if self.bindings.analyst_factory is None:
            raise APIError("analyst_not_configured", "Configure a server-side PromptAnalyst.", 503)
        adapter = self.adapter(data["worker_id"], data["role"])
        with self.runner() as runner:
            result = runner.start(
                original_prompt=data["prompt"],
                worker_id=data["worker_id"],
                role=data["role"],
                prompt_analyst=self.bindings.analyst_factory(),
                adapter=adapter,
                requires_mutation=False,
            )
        return result

    def resume(self, run_id):
        with self.reads() as reads:
            run = reads.run(run_id)
        # A terminal result needs no configured/available adapter (runner owns idempotency).
        terminal = run.status in ("COMPLETED", "FAILED", "DENIED_TRUST")
        adapter = None if terminal else self.adapter(run.worker_id, run.role)
        with self.runner() as runner:
            return runner.resume(run_id, adapter=adapter)

    def resolve(self, run_id, data):
        with self.reads() as reads:
            run = reads.run(run_id)
            questions = reads.questions(run)
        if run.status != "BLOCKED_ON_QUESTIONS":
            raise APIError("run_not_blocked", "Run is not waiting for answers.", 409)
        if data["ambiguity_id"] not in {q["ambiguity_id"] for q in questions}:
            raise APIError("unknown_ambiguity", "Choose a current question from this run.", 409)
        with self.runner() as runner:
            runner.record_user_resolution(
                run_id,
                data["ambiguity_id"],
                data["answer"],
                resolution_kind=ResolutionKind(data["resolution_kind"]),
            )
            return runner.status(run_id)

    # -- repository intelligence (Phase 8.1; read-only, never executes anything) --

    def intelligence_status(self):
        with self.intelligence() as service:
            return asdict(service.status())

    def intelligence_refresh(self):
        with self.intelligence() as service:
            service.inspect(force=True)
            return asdict(service.status())

    def intelligence_query(self, text, limit):
        with self.intelligence() as service:
            candidates, stale = service.query(text, limit=limit)
            return {"stale": stale, "candidates": [asdict(c) for c in candidates]}

    def intelligence_context_pack(self, text, *, max_files, max_bytes, per_file_bytes):
        with self.intelligence() as service:
            pack = service.build_context_pack(
                text, max_files=max_files, max_bytes=max_bytes, per_file_bytes=per_file_bytes,
            )
        if pack is None:
            raise APIError("not_indexed", "Repository has not been indexed yet.", 409)
        return asdict(pack)

    # -- engineering planning (Phase 8.2; planning only — never mutates,
    # executes, or grants any trust/execution authority) ------------------

    @staticmethod
    def _plan_json(record):
        # `asdict()` already recurses through `content` (an
        # `EngineeringPlanContent | None`) into a plain, JSON-serializable
        # dict/None — `PlanState`/`AffectedFileAction` StrEnum members
        # serialize as their plain string value directly (see
        # `planning.models`'s own StrEnum members; no custom encoder needed).
        return asdict(record)

    @staticmethod
    def _job_json(record):
        return asdict(record) | {"status_url": f"/api/planning-jobs/{record.job_id}"}

    def _require_planner_configured(self):
        if self.bindings.planner_factory is None:
            raise APIError("planner_not_configured", "Configure a server-side Planner.", 503)

    def list_plans(self, limit, offset):
        with self.planning() as service:
            return {"plans": [self._plan_json(r) for r in service.list(limit=limit, offset=offset)]}

    def get_plan(self, plan_id):
        with self.planning() as service:
            try:
                return self._plan_json(service.get(plan_id))
            except KeyError:
                raise APIError("not_found", "Plan not found.", 404) from None

    def create_plan(self, data):
        """Durably accepts a new planning job and returns immediately —
        HTTP 202 (see `api.routes.create_plan`). No planner is ever
        invoked on this request's own thread; `self._planning_executor`
        (started once at process startup, never per request) claims and
        executes it in the background."""
        self._require_planner_configured()
        with self.planning() as service:
            job = service.create_job(original_request=data["request"])
        self._planning_executor.notify()
        return self._job_json(job)

    def resume_plan(self, plan_id):
        """Synchronous: `EngineeringPlanningService.resume()` only
        re-evaluates the already-hardened Question Gate against durable
        resolutions — it never invokes a planner, so there is no
        long-running inference here to move to a background job."""
        with self.planning() as service:
            try:
                record = service.resume(plan_id)
            except KeyError:
                raise APIError("not_found", "Plan not found.", 404) from None
        return self._plan_json(record)

    def replan_plan(self, plan_id):
        """Durably accepts a replan job and returns immediately — HTTP
        202, same as `create_plan()`. `replan()` (the synchronous,
        planner-invoking equivalent) is never called from this path."""
        self._require_planner_configured()
        with self.planning() as service:
            try:
                job = service.replan_job(plan_id)
            except KeyError:
                raise APIError("not_found", "Plan not found.", 404) from None
            except ValueError as exc:
                raise APIError("plan_superseded", str(exc), 409) from None
        self._planning_executor.notify()
        return self._job_json(job)

    def list_planning_jobs(self, limit, offset):
        with self.planning() as service:
            jobs = service.list_jobs(limit=limit, offset=offset)
            return {"jobs": [self._job_json(j) for j in jobs]}

    def get_planning_job(self, job_id):
        with self.planning() as service:
            try:
                return self._job_json(service.get_job(job_id))
            except KeyError:
                raise APIError("not_found", "Planning job not found.", 404) from None

    def resolve_plan(self, plan_id, data):
        with self.planning() as service:
            try:
                record = service.get(plan_id)
            except KeyError:
                raise APIError("not_found", "Plan not found.", 404) from None
            if record.state != "NEEDS_INPUT":
                raise APIError("plan_not_blocked", "Plan is not waiting for answers.", 409)
            if data["ambiguity_id"] not in {q["ambiguity_id"] for q in record.questions}:
                raise APIError(
                    "unknown_ambiguity", "Choose a current question from this plan.", 409,
                )
            service.record_user_resolution(
                plan_id, data["ambiguity_id"], data["answer"],
                resolution_kind=ResolutionKind(data["resolution_kind"]),
                source=EvidenceSource.DURABLE_TASK_EVIDENCE,
            )
            return self._plan_json(service.get(plan_id))

    # -- CSLR Permission Engine (Governance Foundation, slice G2) -----------
    #
    # Read-only definitions plus a narrow decision/revocation surface only.
    # There is deliberately no "POST /api/permissions/requests" here: a
    # permission request is created exclusively by trusted backend code
    # (see `permissions.service.PermissionService.request()`), never by an
    # HTTP client -- see `docs/PERMISSIONS_MODEL.md`.

    @staticmethod
    def _permission_definition_json(view):
        return asdict(view)

    @staticmethod
    def _permission_request_json(record):
        return asdict(record)

    @staticmethod
    def _permission_grant_json(record):
        return asdict(record)

    def list_permission_definitions(self):
        with self.permissions() as service:
            definitions = service.definitions()
            return {"definitions": [self._permission_definition_json(d) for d in definitions]}

    def list_permission_requests(self, limit, offset):
        with self.permissions() as service:
            requests = service.list_requests(limit=limit, offset=offset)
            return {"requests": [self._permission_request_json(r) for r in requests]}

    def get_permission_request(self, request_id):
        with self.permissions() as service:
            try:
                return self._permission_request_json(service.get_request(request_id))
            except KeyError:
                raise APIError("not_found", "Permission request not found.", 404) from None

    def decide_permission_request(self, request_id, data):
        with self.permissions() as service:
            try:
                record = service.decide(request_id, data["decision"])
            except KeyError:
                raise APIError("not_found", "Permission request not found.", 404) from None
            except ValueError as exc:
                raise APIError("invalid_decision", str(exc), 400) from None
            return self._permission_request_json(record)

    def list_permission_grants(self, limit, offset):
        with self.permissions() as service:
            grants = service.grants(limit=limit, offset=offset)
            return {"grants": [self._permission_grant_json(g) for g in grants]}

    def revoke_permission_grant(self, grant_id):
        with self.permissions() as service:
            try:
                record = service.revoke(grant_id)
            except KeyError:
                raise APIError("not_found", "Permission grant not found.", 404) from None
            return self._permission_grant_json(record)
