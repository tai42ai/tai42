"""The preset-rename cascade registry — the body behind ``app.tools.register_rename_cascade``
(register / duplicate-raise / snapshot / reset). The rename running each cascade through a live
app is pinned in ``tests/operations/presets/test_rename_delete_ops.py``."""

from __future__ import annotations

import pytest

from tai42_skeleton.tools.rename_cascades import ToolRenameCascadeRegistry


async def _first(_old: str, _new: str) -> None:
    return None


async def _second(_old: str, _new: str) -> None:
    return None


def test_registry_collects_in_registration_order_and_snapshots() -> None:
    reg = ToolRenameCascadeRegistry()
    assert reg.all() == []
    reg.register(_second)
    reg.register(_first)
    assert reg.all() == [_second, _first]
    reg.all().clear()
    assert reg.all() == [_second, _first]


def test_registry_rejects_a_duplicate_cascade() -> None:
    reg = ToolRenameCascadeRegistry()
    reg.register(_first)
    with pytest.raises(ValueError, match="this rename cascade is already registered"):
        reg.register(_first)
    assert reg.all() == [_first]


def test_registry_reset_clears() -> None:
    reg = ToolRenameCascadeRegistry()
    reg.register(_first)
    reg.reset()
    assert reg.all() == []
