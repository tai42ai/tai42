"""C3 / M3 + M4 — the three process kinds resolve ONE absolute multiproc dir
regardless of the working directory each launched from, and backend-executed
increments carry ``runtime="backend"``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import PROBE_TOOLS_TITLE
from tai42_e2e.stack import TaiStack

_FAMILY = "tai_tool_call_count_total"


async def test_three_processes_share_one_dir_across_cwds(fresh_stack: Callable[..., TaiStack], tmp_path: Path) -> None:
    cwds = {}
    for name in ("serve", "backend", "metrics"):
        d = tmp_path / f"cwd-{name}"
        d.mkdir()
        cwds[name] = str(d)
    stack = fresh_stack(cwd_overrides=cwds)

    async with stack.mcp() as mcp:
        await mcp.call_tool("e2e_echo_prometheus_metrics", {"payload": "main"})
        # Execute the counter-wrapped tool inside the backend worker.
        await mcp.call_tool("e2e_echo_prometheus_metrics_sync_task", {"payload": "backend"})

    main_labels = {"name": "e2e_echo", "runtime": "main", "title": PROBE_TOOLS_TITLE}
    backend_labels = {"name": "e2e_echo", "runtime": "backend", "title": PROBE_TOOLS_TITLE}

    async def both_runtimes() -> bool:
        scrape = stack.scrape()
        return scrape.sample(_FAMILY, main_labels) is not None and scrape.sample(_FAMILY, backend_labels) is not None

    # One scrape shows both runtimes -> all three processes resolved one dir.
    await wait_for_async(
        both_runtimes,
        deadline=5.0,
        message="the single scrape never showed both runtime=main and runtime=backend samples",
    )


async def test_backend_runtime_label(core_stack: TaiStack) -> None:
    backend_labels = {"name": "e2e_echo", "runtime": "backend", "title": PROBE_TOOLS_TITLE}
    async with core_stack.mcp() as mcp:
        await mcp.call_tool("e2e_echo_prometheus_metrics_sync_task", {"payload": "backend"})

    async def backend_sample() -> bool:
        return core_stack.scrape().sample(_FAMILY, backend_labels) is not None

    await wait_for_async(
        backend_sample, deadline=5.0, message="no runtime=backend sample after a backend-executed increment"
    )
