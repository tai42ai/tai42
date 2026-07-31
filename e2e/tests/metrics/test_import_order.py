"""C2 / M1 mechanism — the multiproc env must be frozen before the first
``prometheus_client`` import in EVERY entrypoint. The probe returns the frozen
value class from inside each real process; ``MutexValue`` anywhere is the
regression, caught by name."""

from __future__ import annotations

from tai42_e2e.stack import TaiStack


def _payload(result: object) -> dict:
    data = getattr(result, "data", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    assert isinstance(data, dict), f"expected a dict worker-info payload, got {result!r}"
    return data


async def test_value_class_multiproc_in_every_process_kind(core_stack: TaiStack) -> None:
    # Two distinct HTTP workers must both have frozen the mmap backend.
    await core_stack.wait_workers(2)
    async with core_stack.mcp() as mcp:
        # A worker fresh from boot may still hold its self-resync reload gate, so poll
        # past the retriable ``reloading`` rejection.
        http_infos = [_payload(await mcp.call_tool("e2e_worker_info", retry_on_reloading=True)) for _ in range(6)]
        # Run the probe inside the backend-worker process via its sync_task branch.
        backend_info = _payload(await mcp.call_tool("e2e_worker_info_sync_task", {}, retry_on_reloading=True))

    for info in [*http_infos, backend_info]:
        assert info["value_class"] == "MmapedValue", f"non-multiproc value class in {info}"
        assert info["multiproc_dir_env"] == core_stack.metrics_dir, (
            f"process resolved a different metrics dir: {info['multiproc_dir_env']} != {core_stack.metrics_dir}"
        )
