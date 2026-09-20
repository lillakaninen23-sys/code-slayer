"""Explicit injected-worker test harness. Never a production execution API."""

from dataclasses import replace

from code_slayer.coding.pipeline import _run_coding_job


def run_coding_job_for_testing(*args, coder_adapter, repairer_adapter=None, **kwargs):
    # Compatibility for parent scripted tests whose Coder queue also contains repairs.
    class TestRepairAdapter:
        def infer(self, request):
            return coder_adapter.infer(replace(request, role="coder"))

    return _run_coding_job(
        *args,
        coder_adapter=coder_adapter,
        repairer_adapter=repairer_adapter or TestRepairAdapter(),
        **kwargs,
    )
