"""The state-template DETACH gate's referee consult, driven at the operations layer.

A registered detach referee whose answer for the ``(state, template)`` is non-empty BLOCKS
the detach with a 409 that NAMES the referencing binding (the template is left attached); an
empty answer lets the detach proceed; a referee that RAISES fails the detach loudly and the
template stays attached. Generic fixtures only."""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations import states as states_ops


def _manifest() -> Manifest:
    return Manifest.model_validate({})


@pytest.fixture(autouse=True)
def _isolate_referee_registry():
    yield
    instance.app._detach_referee_registry.reset()


async def _blocks(_state: str, _template: str) -> list[str]:
    return ["preset 'p' version 1 binds it"]


async def _allows(_state: str, _template: str) -> list[str]:
    return []


async def _boom(_state: str, _template: str) -> list[str]:
    raise RuntimeError("referee store unreadable")


def _patch_detach(monkeypatch) -> list[tuple[str, str]]:
    """Record calls to the underlying facet detach so a proceeding detach needs no live
    states store — the gate's consult, not the store write, is under test here."""
    detached: list[tuple[str, str]] = []

    async def detach(state, template):
        detached.append((state, template))

    monkeypatch.setattr(instance.app._states_facet, "detach", detach)
    return detached


def test_facet_registers_lists_and_rejects_duplicate() -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            instance.app.tools.register_detach_referee(_allows)
            assert instance.app.tools.detach_referees() == [_allows]
            with pytest.raises(ValueError, match="already registered"):
                instance.app.tools.register_detach_referee(_allows)

    asyncio.run(run())


def test_a_vetoing_referee_blocks_the_detach_and_names_the_holder(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            detached = _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_blocks)
            with pytest.raises(ConflictError, match="preset 'p' version 1 binds it"):
                await states_ops.detach_state_template("status", "t1")
            # Nothing was detached — the veto blocked before the facet call.
            assert detached == []

    asyncio.run(run())


def test_an_allowing_referee_lets_the_detach_proceed(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            detached = _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_allows)
            result = await states_ops.detach_state_template("status", "t1")
            assert result == {"detached": True, "state": "status", "template": "t1"}
            assert detached == [("status", "t1")]

    asyncio.run(run())


def test_a_raising_referee_fails_the_detach_loudly(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            detached = _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_boom)
            with pytest.raises(RuntimeError, match="referee store unreadable"):
                await states_ops.detach_state_template("status", "t1")
            assert detached == []

    asyncio.run(run())
