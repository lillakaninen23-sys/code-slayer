"""Local runtime wiring for `codeslayer serve --runtime-factory
runtime_devstral:create_runtime`.

This is a smoke/bring-up runtime, not the permanent multi-model
architecture: it wires exactly one local OpenAI-compatible model
(Devstral over Ollama) as the Planner, the Prompt Analyst, and the sole
worker execution adapter this process offers. It deliberately does NOT
encode "Devstral = Planner/Coder" as a permanent rule — every role here
is served by the same runtime purely because it is the only runtime
configured today; a future Model/Qualification Registry + Router
(`docs/ROADMAP.md`) is what should decide role -> worker routing once
more than one runtime exists.

Registering this worker (`worker_registrations` below) and returning an
adapter for it grants no trust, no qualification, and no certification
of any kind. `workers.trust.WorkerTrustManager` still gates every real
tool call this worker ever attempts, exactly as for any other worker —
a freshly registered worker starts `LOCKED` and stays `LOCKED` for every
capability until real conformance evidence
(`workers.conformance.run_conformance_suite` +
`workers.promotion.promote_from_conformance`) earns it `GUARDED`.
"""

from code_slayer.api.service import RuntimeBindings, WorkerRegistration
from code_slayer.planning.worker_planner import WorkerAdapterPlanner
from code_slayer.workers.openai_compatible_adapter import (
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
)
from code_slayer.workers.worker_prompt_analyst import WorkerAdapterPromptAnalyst

# The one worker identity this runtime declares. A future multi-model
# runtime would register one of these per configured model/provider
# instead of hardcoding a single id here.
DEVSTRAL_WORKER_ID = "local-devstral-24b"

_CONFIG = OpenAICompatibleConfig(
    base_url="http://192.168.32.8:11436/v1",
    model="devstral:24b",
    timeout=600.0,
    temperature=0.0,
)


def _adapter() -> OpenAICompatibleAdapter:
    # A fresh, stateless adapter per call -- `OpenAICompatibleAdapter`
    # holds no mutable session state worth sharing, and this keeps every
    # factory below independent of the others' lifetimes.
    return OpenAICompatibleAdapter(_CONFIG)


def create_runtime() -> RuntimeBindings:
    def planner_factory():
        return WorkerAdapterPlanner(_adapter(), task_id="devstral-planner", role="planner")

    def analyst_factory():
        return WorkerAdapterPromptAnalyst(
            _adapter(), task_id="devstral-prompt-analyst", role="prompt_analyst",
        )

    def adapter_factory(worker_id: str, role: str):
        # Only the one worker_id this runtime actually registered is
        # ever offered an adapter -- an unknown/unregistered worker_id
        # gets None, never a silent fallback to this runtime by default.
        # `role` does not gate adapter construction: whether this worker
        # is actually trusted to use any capability in that role is
        # `workers.trust.WorkerTrustManager`'s decision, made later,
        # downstream of this factory.
        if worker_id != DEVSTRAL_WORKER_ID:
            return None
        return _adapter()

    return RuntimeBindings(
        analyst_factory=analyst_factory,
        adapter_factory=adapter_factory,
        planner_factory=planner_factory,
        worker_registrations=(
            WorkerRegistration(
                worker_id=DEVSTRAL_WORKER_ID, kind="openai_compatible", network_class="local",
            ),
        ),
    )
