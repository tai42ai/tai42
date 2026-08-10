"""L5 — retention: the conversation-checkpoint sweep over the API.

The sweep runs over the API and reports the memory checkpoint provider as a no-op (it carries
its own process-lifetime state), so the sweep reports a skip rather than deleting.
"""

from __future__ import annotations

from collections.abc import Callable

from _bridge_support import BridgeHarness


async def test_checkpoint_sweep_runs_and_reports_provider(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    result = await bridge.api().post("/api/checkpoints/sweep", json={})
    # The bridge profile pins the memory checkpoint provider — process-lifetime, no swept
    # store — so the sweep is a reported no-op, never a silent one.
    assert result["provider"] == "memory"
    assert result["swept_count"] == 0
    assert "skipped" in result
