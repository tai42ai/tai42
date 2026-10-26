"""The state-template DETACH gate and its preview, driven at the operations layer.

A registered detach referee whose answer for the ``(state, template)`` holds a holder BLOCKS
the detach with a 409 that lists each holder's summary and carries the holders (the template
is left attached); the preview reports exactly the holders the gate refuses on; an empty
answer lets the detach proceed; a referee that RAISES, or answers anything but holders, fails
the detach loudly. A template not attached on the state is a 404 before any referee runs.
Generic fixtures only."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError, NotFoundError
from tai42_skeleton.operations import states as states_ops

from .._fakes.caller_reach import bound_caller

_VERSION_HOLDER = ReferenceHolder(
    kind="preset version", tag="presets", name="p", locator={"version": 1}, summary="preset 'p' version 1"
)
_HOOK_HOLDER = ReferenceHolder(kind="hook", tag="hooks", name="status-hook", summary="hook 'status-hook'")


def _manifest() -> Manifest:
    return Manifest.model_validate({})


@pytest.fixture(autouse=True)
def _isolate_referee_registry():
    yield
    instance.app._detach_referee_registry.reset()


async def _blocks(_state: str, _template: str) -> list[ReferenceHolder]:
    return [_VERSION_HOLDER]


async def _blocks_too(_state: str, _template: str) -> list[ReferenceHolder]:
    return [_HOOK_HOLDER]


async def _allows(_state: str, _template: str) -> list[ReferenceHolder]:
    return []


async def _boom(_state: str, _template: str) -> list[ReferenceHolder]:
    raise RuntimeError("referee store unreadable")


async def _plain_line(_state: str, _template: str) -> list[str]:
    return ["a plain line"]


def _patch_detach(monkeypatch, attached: set[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """Fake the facet's attachment read and detach so no live states store is needed.

    ``attached`` is the set of ``(state, template)`` pairs the read reports (default: only
    ``("status", "t1")``); a detach is recorded, not written.
    """
    pairs = {("status", "t1")} if attached is None else attached
    detached: list[tuple[str, str]] = []

    async def list_attachments(state=None, *, template=None):
        return [{"state": state, "template": template}] if (state, template) in pairs else []

    async def detach(state, template):
        detached.append((state, template))

    monkeypatch.setattr(instance.app._states_facet, "list_attachments", list_attachments)
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


@pytest.mark.usefixtures("admin_caller")
def test_a_vetoing_referee_blocks_the_detach_and_names_the_holder(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            detached = _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_blocks)
            with pytest.raises(ConflictError) as refused:
                await states_ops.detach_state_template("status", "t1")
            assert str(refused.value) == (
                "template 't1' on state 'status' cannot be detached — held by:\n"
                "- preset 'p' version 1\n"
                "change those bindings first"
            )
            assert refused.value.extra == {"holders": [_VERSION_HOLDER.model_dump()]}
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


@pytest.mark.usefixtures("admin_caller")
def test_the_preview_reports_exactly_the_holders_the_gate_refuses_on(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_blocks)
            instance.app.tools.register_detach_referee(_blocks_too)
            preview = await states_ops.state_template_referees("status", "t1")
            assert preview == {
                "action": "detach",
                "holders": [_VERSION_HOLDER.model_dump(), _HOOK_HOLDER.model_dump()],
            }
            with pytest.raises(ConflictError) as refused:
                await states_ops.detach_state_template("status", "t1")
            assert refused.value.extra == {"holders": preview["holders"]}

    asyncio.run(run())


def test_a_role_member_reads_the_holders_its_role_reads_and_a_count_of_the_others(monkeypatch) -> None:
    withheld = {
        "kind": "preset version",
        "tag": "presets",
        "name": "presets",
        "locator": {"count": 1},
        "summary": "preset version: 1 not readable with your role",
    }

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            detached = _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_blocks)
            instance.app.tools.register_detach_referee(_blocks_too)
            preview = await states_ops.state_template_referees("status", "t1")
            assert preview == {"action": "detach", "holders": [withheld, _HOOK_HOLDER.model_dump()]}
            with pytest.raises(ConflictError) as refused:
                await states_ops.detach_state_template("status", "t1")
            assert str(refused.value) == (
                "template 't1' on state 'status' cannot be detached — held by:\n"
                "- preset version: 1 not readable with your role\n"
                "- hook 'status-hook'\n"
                "change those bindings first; the others need someone with access to 'presets'"
            )
            assert refused.value.extra == {"holders": preview["holders"]}
            assert detached == []

    with bound_caller(monkeypatch, {"hooks": "read", "states": "write"}):
        asyncio.run(run())


def test_the_preview_of_an_unheld_attachment_is_empty(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_allows)
            assert await states_ops.state_template_referees("status", "t1") == {"action": "detach", "holders": []}

    asyncio.run(run())


def test_an_unattached_template_is_a_404_before_any_referee_runs(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            detached = _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_boom)
            message = "template 't2' is not attached on state 'status'"
            with pytest.raises(NotFoundError, match=message):
                await states_ops.state_template_referees("status", "t2")
            with pytest.raises(NotFoundError, match=message):
                await states_ops.detach_state_template("status", "t2")
            assert detached == []

    asyncio.run(run())


def test_a_referee_answering_anything_but_holders_fails_the_detach_naming_it(monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            detached = _patch_detach(monkeypatch)
            instance.app.tools.register_detach_referee(_plain_line)  # type: ignore[arg-type]
            with pytest.raises(TypeError, match=r"_plain_line answered \['a plain line'\]"):
                await states_ops.detach_state_template("status", "t1")
            with pytest.raises(TypeError, match="a referee answers a list of ReferenceHolder"):
                await states_ops.state_template_referees("status", "t1")
            assert detached == []

    asyncio.run(run())


def test_the_preview_and_the_gate_are_not_supported_with_states_off(monkeypatch) -> None:
    from tai42_skeleton.operations import NotSupportedError

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._detach_referee_registry.reset()
            instance.app.tools.register_detach_referee(_boom)
            with pytest.raises(NotSupportedError):
                await states_ops.state_template_referees("status", "t1")
            with pytest.raises(NotSupportedError):
                await states_ops.detach_state_template("status", "t1")

    asyncio.run(run())
