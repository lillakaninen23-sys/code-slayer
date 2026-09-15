"""The application/service layer composing Phase 1-7.6 into one
persistent, resumable local worker runner (Phase 7.7 —
`docs/ROADMAP.md#local-worker-runtime`).

`local_worker_runner.LocalWorkerRunner` is the intended, single
application contract a future CLI or WebUI calls — never a second
orchestration implementation. See that module's own docstring for the
full composition and its documented boundaries/limitations.
"""

from code_slayer.runner.local_worker_runner import (
    LocalWorkerRunner,
    RunResult,
    RunStatus,
)

__all__ = ["LocalWorkerRunner", "RunResult", "RunStatus"]
