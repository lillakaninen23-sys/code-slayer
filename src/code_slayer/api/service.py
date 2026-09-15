"""Application wiring and safe read models. Only the runner performs actions.

Factories are trusted host configuration, never HTTP inputs. Each request owns
its connections and adapters; no SQLite connection crosses a server thread.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from code_slayer.api.reads import ReadModels
from code_slayer.repo import identity
from code_slayer.runner import LocalWorkerRunner
from code_slayer.workers.prompt_analysis import PromptAnalyst
from code_slayer.workers.protocol import WorkerAdapter
from code_slayer.workers.question_gate import ResolutionKind


class APIError(Exception):
    def __init__(self, code, message, status=400, retryable=False):
        self.code, self.message = code, message
        self.status, self.retryable = status, retryable


@dataclass(frozen=True)
class RuntimeBindings:
    """Server-owned factories; registration and trust remain existing backend work."""

    analyst_factory: Callable[[], PromptAnalyst] | None = None
    adapter_factory: Callable[[str, str], WorkerAdapter | None] | None = None


class ApplicationService:
    def __init__(self, repo_path, *, state_root=None, bindings=None):
        self.repo_path = Path(repo_path).resolve()
        self.state_root = state_root
        self.bindings = bindings or RuntimeBindings()
        # Initialize through the real application service, including existing migrations.
        with self.runner():
            pass
        self.identity = identity.resolve(self.repo_path, create=False)

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
