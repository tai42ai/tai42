"""Retention: the checkpoint sweep over the API.

The bridge profile pins the in-process ``memory`` checkpoint provider: a thread lives with its
process, so the sweep runs its finished horizon (nothing is marked finished here) and reports
the waiting horizon as skipped rather than deleting.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ._bridge_support import BridgeHarness

pytestmark = pytest.mark.needs("kind:identity", "setting:checkpoint:memory", "setting:router:checkpoints")


async def test_checkpoint_sweep_runs_and_reports_provider(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    result = await bridge.api().post("/api/checkpoints/sweep", json={})
    # The waiting horizon on the memory provider is the process lifetime — a reported skip,
    # never a silent one.
    assert result["provider"] == "memory"
    assert result["swept_count"] == 0
    assert result["finished_swept"] == []
    assert result["waiting_swept"] == []
    assert result["skipped"] == "waiting horizon: provider 'memory' keeps threads for the process lifetime"
