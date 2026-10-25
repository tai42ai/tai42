"""The preset-delete cascade registry — the body behind ``app.tools.register_delete_cascade``
(register / duplicate-raise / snapshot / reset). The delete running each cascade through a live
app is pinned in ``tests/operations/test_delete_referees_ops.py``."""

from __future__ import annotations

import pytest

from tai42_skeleton.tools.delete_cascades import ToolDeleteCascadeRegistry


async def _first(_name: str) -> None:
    return None


async def _second(_name: str) -> None:
    return None


def test_registry_collects_in_registration_order_and_snapshots() -> None:
    reg = ToolDeleteCascadeRegistry()
    assert reg.all() == []
    reg.register(_second)
    reg.register(_first)
    assert reg.all() == [_second, _first]
    reg.all().clear()
    assert reg.all() == [_second, _first]


def test_registry_rejects_a_duplicate_cascade() -> None:
    reg = ToolDeleteCascadeRegistry()
    reg.register(_first)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_first)
    assert reg.all() == [_first]


def test_registry_reset_clears() -> None:
    reg = ToolDeleteCascadeRegistry()
    reg.register(_first)
    reg.reset()
    assert reg.all() == []
