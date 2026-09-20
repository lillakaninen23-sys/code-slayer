"""Application wiring and safe read models. Only the runner performs actions.

Factories are trusted host configuration, never HTTP inputs. Each request owns
its connections and adapters; no SQLite connection crosses a server thread.
"""

import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from code_slayer.api.reads import ReadModels
from code_slayer.finalization.dispatcher import TaskLifecycleExecutor
from code_slayer.intelligence import RepositoryIntelligenceService
from code_slayer.permissions.service import PermissionService
from code_slayer.planning.executor import PlanningJobExecutor
from code_slayer.planning.routing import RoutingOutcome, select_planner_route
from code_slayer.planning.service import (
    EngineeringPlanningService,
    PlannerRouteBindingRejectedError,
)
from code_slayer.repo import identity
from code_slayer.runner import LocalWorkerRunner
from code_slayer.security.certification_executor import CertificationJobExecutor
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    CertificationBlocked,
    CertificationConflict,
    CertificationService,
    RoleEvaluationTarget,
)
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
    # LEGACY, non-authoritative (H.4). A zero-argument Planner factory
    # with no worker identity at all -- `/api/plans`/`PlanningJobExecutor`
    # never read this field for production routing and never infer a
    # worker identity from it. Kept only so dev/test wiring that still
    # constructs a `RuntimeBindings` with just this field continues to
    # import/construct without error; it authorizes nothing on its own.
    # See `planner_factory_for_worker` for the real, worker-bound path.
    planner_factory: Callable[[], object] | None = None
    # H.4: the real, worker-bound production Planner factory —
    # `Callable[[worker_id, job_id], planning.planner.Planner]`. The
    # ONE thing `planning.executor.PlanningJobExecutor` ever calls to
    # construct a Planner for a claimed job, and only after that job's
    # own durable route binding has been freshly revalidated
    # (`planning.routing.revalidate_route_binding()`) against the EXACT
    # `worker_id` it names — never a caller-chosen worker, never this
    # codebase's own zero-argument `planner_factory` above.
    planner_factory_for_worker: Callable[[str, str], object] | None = None
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
    baseline_certification_targets: tuple[BaselineCertificationTarget, ...] = ()
    role_evaluation_targets: tuple[RoleEvaluationTarget, ...] = ()
    certification_poll_interval_seconds: float = 1.0


class ApplicationService:
    def __init__(
        self, repo_path, *, state_root=None, bindings=None,
        config_path=None, load_persistent_config=False,
    ):
        self.repo_path = Path(repo_path).resolve()
        self.state_root = state_root
        self._config_path = config_path
        self._load_persistent_config = load_persistent_config
        self._explicit_bindings = bindings or RuntimeBindings()
        self._started_at = time.monotonic()
        self._started_wall = time.time()
        self.process_identity = self._capture_process_identity()
        self.process_commit = self.process_identity.commit
        self.process_commit_source = self.process_identity.commit_source
        self.process_source_dirty = self.process_identity.dirty
        self.process_source_state = self.process_identity.state
        self.bindings = self._compose_bindings()
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
        # when a worker-bound Planner factory is actually configured
        # (H.4: `planner_factory_for_worker`, never the legacy zero-arg
        # `planner_factory`); when it is not, POST /api/plans fails
        # explicitly (see `_require_planner_configured()`) rather than
        # accepting a job nothing could ever execute. `bindings_factory`
        # (H.4 review finding) mirrors `_certification_executor`'s own
        # pattern below exactly: when persistent config is in play, the
        # executor re-derives current `baseline_certification_targets`/
        # `role_evaluation_targets`/`planner_factory_for_worker` FRESH on
        # every single job execution, never a snapshot frozen at this
        # one construction instant.
        self._planning_executor = None
        if self.bindings.planner_factory_for_worker is not None:
            self._planning_executor = PlanningJobExecutor(
                self.repo_path,
                planner_factory_for_worker=self.bindings.planner_factory_for_worker,
                baseline_targets=self.bindings.baseline_certification_targets,
                role_targets=self.bindings.role_evaluation_targets,
                bindings_factory=(
                    self._compose_bindings if self._load_persistent_config else None
                ),
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
        self._certification_executor = CertificationJobExecutor(
            self.identity.repo_id,
            self.identity.worktree_id,
            state_root=self.state_root,
            targets=self.bindings.baseline_certification_targets,
            role_targets=self.bindings.role_evaluation_targets,
            poll_interval_seconds=self.bindings.certification_poll_interval_seconds,
            bindings_factory=(
                self._compose_bindings if self._load_persistent_config else None
            ),
        )
        self._certification_executor.start()

    def _capture_process_identity(self):
        """Retain checkout HEAD and dirtiness at process start. Not re-read later.

        Editable installs can load dirty/untracked source while HEAD is
        unchanged. A dirty start is never labelled VERIFIED.
        """
        from code_slayer.admin.process import inspect_checkout

        return inspect_checkout(self.repo_path)

    def current_checkout_identity(self):
        from code_slayer.admin.process import inspect_checkout

        return inspect_checkout(self.repo_path)

    def current_checkout_head(self) -> tuple[str | None, str]:
        identity = self.current_checkout_identity()
        if identity.commit is None:
            return None, "UNVERIFIED"
        return identity.commit, "OBSERVED"

    def _compose_bindings(self) -> RuntimeBindings:
        explicit = self._explicit_bindings
        if not self._load_persistent_config:
            return explicit
        from code_slayer.config.bindings import runtime_bindings_from_config
        from code_slayer.config.store import load_config

        from_cfg = runtime_bindings_from_config(load_config(path=self._config_path))
        return replace(
            from_cfg,
            analyst_factory=explicit.analyst_factory,
            adapter_factory=explicit.adapter_factory,
            planner_factory=explicit.planner_factory,
            planning_max_workers=explicit.planning_max_workers,
            planning_poll_interval_seconds=explicit.planning_poll_interval_seconds,
            lifecycle_poll_interval_seconds=explicit.lifecycle_poll_interval_seconds,
            certification_poll_interval_seconds=explicit.certification_poll_interval_seconds,
        )

    def refresh_persistent_workers(self) -> None:
        self.bindings = self._compose_bindings()
        with self.runner() as runner:
            for registration in self.bindings.worker_registrations:
                runner.register_worker(
                    worker_id=registration.worker_id, kind=registration.kind,
                    network_class=registration.network_class,
                )

    def persistent_config(self):
        from code_slayer.config.store import load_config

        return load_config(path=self._config_path)

    def save_persistent_config(self, config) -> None:
        from code_slayer.config.store import save_config

        save_config(config, path=self._config_path)
        self.refresh_persistent_workers()

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
        self._certification_executor.stop()

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
            worker = reads.worker(data["worker_id"])
            if worker is None:
                raise APIError("unknown_worker", "Worker is not registered.", 404)
            # H.3: defense-in-depth surfacing only -- `LocalWorkerRunner.
            # start()` is the authoritative gate below the HTTP layer and
            # refuses independently; this check exists so an archived
            # worker fails fast, before configuring an adapter or opening
            # the runner, with a clean 409 rather than a 201 whose body
            # says `reason: worker_archived`.
            if worker.lifecycle_state != "ACTIVE":
                raise APIError("worker_archived", "Worker is administratively archived.", 409)
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

    def _current_planning_bindings(self) -> RuntimeBindings:
        """H.4 review finding: route SELECTION (`create_plan()`/
        `replan_plan()`) must use a FRESH current config/bindings
        snapshot, never `self.bindings` as captured at the last config
        save — persistent config can change between then and now.
        Mirrors `planning.executor.PlanningJobExecutor._routing_inputs()`'s
        own freshness discipline exactly, just on the HTTP request
        thread rather than the background dispatcher thread. When this
        process was never configured to load persistent config at all
        (`load_persistent_config=False`, e.g. most dev/test wiring),
        `self.bindings` already IS the only, unchanging source of truth
        -- no separate resolution is needed or possible."""
        if self._load_persistent_config:
            return self._compose_bindings()
        return self.bindings

    def _require_planner_configured(self, bindings: RuntimeBindings | None = None) -> None:
        bindings = bindings if bindings is not None else self.bindings
        if bindings.planner_factory_for_worker is None:
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

    def _select_planner_route(self, bindings: RuntimeBindings, service: EngineeringPlanningService):
        """The ONE place an HTTP request selects a NEW job's Planner
        route (H.4) — `planning.routing.select_planner_route()`,
        against `bindings`' FRESH `baseline_certification_targets`/
        `role_evaluation_targets` and `service`'s own production
        connection. Raises `APIError` (503) for zero/multiple eligible
        candidates; returns the `PlannerRouteBinding` plus the exact
        targets used, so the caller can pass the SAME snapshot into
        `create_job()`/`replan_job()`'s own atomic re-verification."""
        selection = select_planner_route(
            service.production_conn(),
            baseline_targets=bindings.baseline_certification_targets,
            role_targets=bindings.role_evaluation_targets,
        )
        if selection.outcome != RoutingOutcome.SELECTED:
            raise APIError(
                selection.outcome.value,
                "No single eligible Planner worker is currently available.", 503,
            )
        return selection.binding

    def create_plan(self, data):
        """Durably accepts a new planning job and returns immediately —
        HTTP 202 (see `api.routes.create_plan`). No planner is ever
        invoked on this request's own thread; `self._planning_executor`
        (started once at process startup, never per request) claims and
        executes it in the background.

        H.4: the server (never the client — `data` has no worker-
        identifying field at all, enforced by `api.routes.body()`'s own
        closed-set spec) selects the exact Planner worker for this new
        job, from a FRESH current config/bindings snapshot
        (`_current_planning_bindings()`), then durably binds the job to
        it. `EngineeringPlanningService.create_job()`'s own atomic,
        in-transaction re-verification (`PlannerRouteBindingRejectedError`)
        is the final, genuinely-atomic guard against a certificate/
        lifecycle change landing in the gap between that selection and
        this request's own write."""
        bindings = self._current_planning_bindings()
        self._require_planner_configured(bindings)
        with self.planning() as service:
            binding = self._select_planner_route(bindings, service)
            try:
                job = service.create_job(
                    original_request=data["request"], route_binding=binding,
                    baseline_targets=bindings.baseline_certification_targets,
                    role_targets=bindings.role_evaluation_targets,
                )
            except PlannerRouteBindingRejectedError as exc:
                raise APIError(
                    exc.reason, "The selected Planner worker is no longer eligible.", 409,
                ) from None
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
        planner-invoking equivalent) is never called from this path.

        H.4: a replan job gets its OWN, freshly and independently
        selected route binding — never inherited from the predecessor
        plan's own prior job. Route-selection failure (zero/multiple
        eligible candidates, or the atomic in-transaction re-
        verification refusing) happens BEFORE the predecessor plan is
        ever marked `SUPERSEDED` (`EngineeringPlanningService.
        replan_job()`'s own atomicity) — a failed replan attempt leaves
        the original plan completely untouched."""
        bindings = self._current_planning_bindings()
        self._require_planner_configured(bindings)
        with self.planning() as service:
            binding = self._select_planner_route(bindings, service)
            try:
                job = service.replan_job(
                    plan_id, route_binding=binding,
                    baseline_targets=bindings.baseline_certification_targets,
                    role_targets=bindings.role_evaluation_targets,
                )
            except KeyError:
                raise APIError("not_found", "Plan not found.", 404) from None
            except ValueError as exc:
                raise APIError("plan_superseded", str(exc), 409) from None
            except PlannerRouteBindingRejectedError as exc:
                raise APIError(
                    exc.reason, "The selected Planner worker is no longer eligible.", 409,
                ) from None
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

    @contextmanager
    def certification(self):
        bindings = self._compose_bindings()
        service = CertificationService(
            self.identity.repo_id,
            self.identity.worktree_id,
            state_root=self.state_root,
            targets=bindings.baseline_certification_targets,
            role_targets=bindings.role_evaluation_targets,
        )
        try:
            yield service
        finally:
            service.close()

    def list_certification_workers(self):
        with self.certification() as service:
            return {
                "environment": "VALIDATION",
                "workers": service.list_workers(),
            }

    def get_certification_worker(self, worker_id):
        with self.certification() as service:
            try:
                return service.worker_detail(worker_id)
            except KeyError:
                raise APIError("not_found", "Worker is not registered.", 404) from None

    def certification_preflight(self, worker_id):
        with self.certification() as service:
            try:
                return service.run_preflight(worker_id)
            except KeyError:
                raise APIError("not_found", "Worker is not registered.", 404) from None
            except CertificationConflict as exc:
                raise APIError(
                    exc.code, "A certification run is already in progress.", 409,
                ) from None

    def start_baseline_certification(self, worker_id):
        with self.certification() as service:
            try:
                result = service.start_baseline_run(worker_id)
            except KeyError:
                raise APIError("not_found", "Worker is not registered.", 404) from None
            except CertificationConflict as exc:
                raise APIError(
                    exc.code, "A certification run is already in progress.", 409,
                ) from None
            except CertificationBlocked as exc:
                raise APIError(
                    exc.code, "Preflight must succeed before certification.", 409,
                ) from None
        self._certification_executor.notify()
        return result

    def promote_baseline_certification(self, worker_id):
        with self.certification() as service:
            try:
                return service.promote_to_production(worker_id)
            except KeyError:
                raise APIError("not_found", "Worker is not registered.", 404) from None
            except CertificationBlocked as exc:
                raise APIError(
                    exc.code, "Promotion requirements were not met.", 409,
                ) from None

    def get_certification_run(self, run_id):
        with self.certification() as service:
            try:
                return service.get_run(run_id)
            except KeyError:
                raise APIError("not_found", "Certification run not found.", 404) from None

    def get_certification_evidence(self, run_id):
        from code_slayer.planning.qualification_evidence import QualificationEvidenceError
        from code_slayer.security.evidence import SecurityEvaluationEvidenceError

        with self.certification() as service:
            try:
                return service.run_evidence(run_id)
            except KeyError:
                raise APIError("not_found", "Certification run not found.", 404) from None
            except CertificationBlocked as exc:
                raise APIError(exc.code, "Evidence is not available for this run.", 409) from None
            except (SecurityEvaluationEvidenceError, QualificationEvidenceError) as exc:
                raise APIError(exc.reason, "Evidence verification failed.", 409) from None

    def certification_planner_preflight(self, worker_id):
        with self.certification() as service:
            try:
                return service.run_planner_preflight(worker_id)
            except KeyError:
                raise APIError("not_found", "Worker is not registered.", 404) from None
            except CertificationConflict as exc:
                raise APIError(
                    exc.code, "A certification run is already in progress.", 409,
                ) from None

    def start_planner_certification(self, worker_id):
        with self.certification() as service:
            try:
                result = service.start_planner_certification(worker_id)
            except KeyError:
                raise APIError("not_found", "Worker is not registered.", 404) from None
            except CertificationConflict as exc:
                raise APIError(
                    exc.code, "A certification run is already in progress.", 409,
                ) from None
            except CertificationBlocked as exc:
                raise APIError(
                    exc.code, "Preflight must succeed before certification.", 409,
                ) from None
        self._certification_executor.notify()
        return result

    def get_certification_history(self, worker_id):
        with self.certification() as service:
            try:
                service.worker_summary(worker_id)
            except KeyError:
                raise APIError("not_found", "Worker is not registered.", 404) from None
            return service.history(worker_id)

    def admin(self):
        from code_slayer.api.admin import AdminFacade

        return AdminFacade(self)
