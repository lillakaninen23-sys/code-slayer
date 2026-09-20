"""Server-owned background dispatcher for Certification Center runs.

HTTP client lifetime has no authority: once a run is QUEUED, this
process executes it even if the browser disconnects. At most one
certification run executes at a time.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    CertificationService,
    RoleEvaluationTarget,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class CertificationJobExecutor:
    def __init__(
        self,
        repo_id: str,
        worktree_id: str,
        *,
        state_root=None,
        targets: tuple[BaselineCertificationTarget, ...] = (),
        role_targets: tuple[RoleEvaluationTarget, ...] = (),
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        bindings_factory=None,
    ) -> None:
        self._repo_id = repo_id
        self._worktree_id = worktree_id
        self._state_root = state_root
        self._targets = targets
        self._role_targets = role_targets
        self._bindings_factory = bindings_factory
        self._poll_interval = poll_interval_seconds
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="certification-job")
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._dispatcher_thread: threading.Thread | None = None

    def _service(self) -> CertificationService:
        targets = self._targets
        role_targets = self._role_targets
        if self._bindings_factory is not None:
            bindings = self._bindings_factory()
            targets = bindings.baseline_certification_targets
            role_targets = bindings.role_evaluation_targets
        return CertificationService(
            self._repo_id,
            self._worktree_id,
            state_root=self._state_root,
            targets=targets,
            role_targets=role_targets,
        )

    def start(self) -> None:
        if self._dispatcher_thread is not None:
            return
        self._dispatch_once()
        self._dispatcher_thread = threading.Thread(
            target=self._loop, name="certification-job-dispatcher", daemon=True,
        )
        self._dispatcher_thread.start()

    def notify(self) -> None:
        self._wakeup.set()

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        if self._dispatcher_thread is not None:
            self._dispatcher_thread.join(timeout=5)
        self._pool.shutdown(wait=False)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wakeup.wait(timeout=self._poll_interval)
            self._wakeup.clear()
            if self._stop.is_set():
                return
            self._dispatch_once()

    def _dispatch_once(self) -> None:
        service = None
        try:
            service = self._service()
            claimable = service.claimable_run_ids()
        except Exception:
            logger.exception("certification dispatcher: discovery failed")
            return
        finally:
            if service is not None:
                service.close()
        for run_id in claimable:
            with self._lock:
                if run_id in self._in_flight or self._in_flight:
                    continue
                self._in_flight.add(run_id)
            self._pool.submit(self._run_one, run_id)

    def _run_one(self, run_id: str) -> None:
        service = None
        claimed = None
        try:
            service = self._service()
            claimed = service.claim_run(run_id)
            if claimed is None:
                return
            service.execute_claimed_run(claimed)
        except Exception:
            logger.exception("certification dispatcher: execution of %s failed", run_id)
            if service is not None and claimed is not None:
                try:
                    service.fail_claimed_run(claimed, "internal_error")
                except Exception:
                    logger.exception("certification dispatcher: fail-closed finish failed")
        finally:
            if service is not None:
                service.close()
            with self._lock:
                self._in_flight.discard(run_id)
