"""The state-template detach referee registry — the body behind
``app.tools.register_detach_referee`` (register / duplicate-raise / snapshot / reset). The
detach-gate consult through a live app is pinned in
``tests/operations/test_detach_referees_ops.py``."""

from __future__ import annotations

import pytest

from tai42_skeleton.tools.detach_referees import StateTemplateDetachRefereeRegistry


async def _empty(_state: str, _template: str) -> list[str]:
    return []


async def _holder(_state: str, _template: str) -> list[str]:
    return ["a preset version binds it"]


def test_registry_collects_and_snapshots() -> None:
    reg = StateTemplateDetachRefereeRegistry()
    assert reg.all() == []
    reg.register(_empty)
    reg.register(_holder)
    assert reg.all() == [_empty, _holder]
    reg.all().clear()  # ``all()`` is a snapshot copy
    assert reg.all() == [_empty, _holder]


def test_registry_rejects_duplicate_provider() -> None:
    reg = StateTemplateDetachRefereeRegistry()
    reg.register(_empty)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_empty)


def test_registry_reset_clears() -> None:
    reg = StateTemplateDetachRefereeRegistry()
    reg.register(_empty)
    reg.reset()
    assert reg.all() == []
