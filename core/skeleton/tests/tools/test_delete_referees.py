"""The preset-delete referee registry — the body behind ``app.tools.register_delete_referee``
(register / duplicate-raise / snapshot / reset). The delete-gate consult through a live app
is pinned in ``tests/operations/test_delete_referees_ops.py``."""

from __future__ import annotations

import pytest

from tai42_skeleton.tools.delete_referees import ToolDeleteRefereeRegistry


async def _empty(_name: str) -> list[str]:
    return []


async def _holder(_name: str) -> list[str]:
    return ["a node binding references it"]


def test_registry_collects_and_snapshots() -> None:
    reg = ToolDeleteRefereeRegistry()
    assert reg.all() == []
    reg.register(_empty)
    reg.register(_holder)
    assert reg.all() == [_empty, _holder]
    reg.all().clear()  # ``all()`` is a snapshot copy
    assert reg.all() == [_empty, _holder]


def test_registry_rejects_duplicate_provider() -> None:
    reg = ToolDeleteRefereeRegistry()
    reg.register(_empty)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_empty)


def test_registry_reset_clears() -> None:
    reg = ToolDeleteRefereeRegistry()
    reg.register(_empty)
    reg.reset()
    assert reg.all() == []
