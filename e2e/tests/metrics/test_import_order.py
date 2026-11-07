"""The multiproc env must be frozen before the first ``prometheus_client`` import in EVERY
entrypoint. The mmap value backend writes its counters to per-process files named by the
writer's pid in the shared multiproc directory, so every worker on the bus census having its
own file set there is that worker's proof, read per worker; the in-process mutex backend
writes none. The probe's frozen value class names the regression (``MutexValue``) in the
process that answers it."""

from __future__ import annotations

import os

import pytest

from tai42_e2e import wait_for
from tai42_e2e.readiness import expected_serve_workers
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:backend", "probe-tools", "files", "topology:multiworker")


def _payload(result: object) -> dict:
    data = getattr(result, "data", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    assert isinstance(data, dict), f"expected a dict worker-info payload, got {result!r}"
    return data


def _without_a_file_set(stack: TaiStack) -> list[str]:
    """Every serve and backend worker on the census whose pid names no mmap file in the
    stack's multiproc directory, by kind, name and pid."""
    files = os.listdir(stack.metrics_dir)
    return [
        f"{w.kind} worker {w.name} (pid {w.pid})"
        for w in stack.census()
        if w.kind in ("serve", "backend") and not any(f.endswith(f"_{w.pid}.db") for f in files)
    ]


async def test_value_class_multiproc_in_every_process_kind(core_stack: TaiStack) -> None:
    workers = core_stack.census()
    serve = [w for w in workers if w.kind == "serve"]
    assert len(serve) == expected_serve_workers(core_stack), f"the census does not hold the serve fleet: {workers}"
    assert any(w.kind == "backend" for w in workers), f"the census holds no backend worker: {workers}"

    # Every process runs the hook-run sweep, whose first tick writes its counters, so each
    # worker's file set appears within a sweep interval of its boot.
    missing: list[str] = []

    def every_worker_wrote() -> bool:
        missing[:] = _without_a_file_set(core_stack)
        return not missing

    wait_for(
        every_worker_wrote,
        deadline=30.0,
        message=lambda: f"no mmap file set in {core_stack.metrics_dir} for: {'; '.join(missing)}",
    )

    async with core_stack.mcp() as mcp:
        http_info = _payload(await mcp.call_tool("e2e_worker_info"))
        # Run the probe inside the backend-worker process via its sync_task branch.
        backend_info = _payload(await mcp.call_tool("e2e_worker_info_sync_task", {}))

    for info in (http_info, backend_info):
        assert info["value_class"] == "MmapedValue", f"non-multiproc value class in {info}"
        assert info["multiproc_dir_env"] == core_stack.metrics_dir, (
            f"process resolved a different metrics dir: {info['multiproc_dir_env']} != {core_stack.metrics_dir}"
        )
