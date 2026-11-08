"""Oracles for the name-lifecycle doors: rename typed-store / compensate branches, the rename
cascades (their order, their per-tool-pair calls, the undo when one raises) and the
conflicted-record hard-delete failure path."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetExistsError, PresetNameConflictError
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app import instance
from tai42_skeleton.operations import BadRequestError, ConflictError
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.operations.errors import OperationFailedError
from tai42_skeleton.operations.presets import fanout

from .conftest import _create, _manifest

Event = tuple[str, ...]


@pytest.fixture(autouse=True)
def _isolate_rename_seam_registries():
    """The rename seams' registries live on the app singleton; clear them after every test so a
    test-registered provider never leaks into another test."""
    yield
    instance.app._rename_cascade_registry.reset()
    instance.app._rename_referee_registry.reset()


def _recording_cascade(
    label: str, events: list[Event], *, raise_on: str | None = None, raise_back_to: str | None = None
) -> Callable[[str, str], Awaitable[None]]:
    """A rename cascade that records each call as ``(label, old, new)``.

    It raises ``store down`` when called for ``raise_on`` and ``store locked`` when called to re-key a name
    back to ``raise_back_to``.
    """

    async def cascade(old: str, new: str) -> None:
        events.append((label, old, new))
        if old == raise_on:
            raise RuntimeError("store down")
        if new == raise_back_to:
            raise RuntimeError("store locked")

    return cascade


def _record_rename_steps(
    monkeypatch: pytest.MonkeyPatch,
    events: list[Event],
    *,
    re_point_error: str | None = None,
    unbind_error: str | None = None,
) -> None:
    """Record every bind, unbind and store move the rename makes, in order, delegating to the real ones.

    ``re_point_error`` makes the store move back from ``v2`` to ``greet`` raise it; ``unbind_error`` makes
    unbinding ``v2`` raise it.
    """
    mgr = instance.app.preset_manager
    real_reload, real_remove = mgr.reload, mgr.remove
    store_cls = type(instance.app.presets.store)
    real_store_rename = store_cls.rename_preset

    async def reload(name: str) -> None:
        events.append(("reload", name))
        await real_reload(name)

    async def remove(name: str) -> None:
        events.append(("remove", name))
        if unbind_error is not None and name == "v2":
            raise RuntimeError(unbind_error)
        await real_remove(name)

    async def store_rename(self, old: str, new: str):
        events.append(("store", old, new))
        if re_point_error is not None and (old, new) == ("v2", "greet"):
            raise RuntimeError(re_point_error)
        return await real_store_rename(self, old, new)

    monkeypatch.setattr(mgr, "reload", reload)
    monkeypatch.setattr(mgr, "remove", remove)
    monkeypatch.setattr(store_cls, "rename_preset", store_rename)


def _record_fanout(monkeypatch: pytest.MonkeyPatch, events: list[Event]) -> None:
    """Record every fan-out the rename starts, delegating to the real one."""
    real_reload, real_remove = fanout._fanout_reload, fanout._fanout_remove

    async def fanout_reload(name, census):
        events.append(("fanout_reload", name))
        return await real_reload(name, census)

    async def fanout_remove(name, census):
        events.append(("fanout_remove", name))
        return await real_remove(name, census)

    monkeypatch.setattr(fanout, "_fanout_reload", fanout_reload)
    monkeypatch.setattr(fanout, "_fanout_remove", fanout_remove)


# -- rename: new-name validity precedes every existence/state check ----------


def test_rename_invalid_new_name_precedes_not_found_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A MISSING preset with an INVALID new name is a 400 "invalid preset name",
            # not a 404: new-name tool-name safety is validated FIRST, before the
            # existence check — name validity precedes not-found.
            with pytest.raises(BadRequestError, match="invalid preset name"):
                await preset_ops.rename_preset(name="missing", new_name="a/b")

    asyncio.run(run())


# -- rename: typed store errors + compensate branches ------------------------


def test_rename_store_exists_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def boom(self, a, b):
                raise PresetExistsError(b)

            monkeypatch.setattr(type(instance.app.presets.store), "rename_preset", boom)
            with pytest.raises(ConflictError, match="already exists"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


def test_rename_store_name_conflict_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def boom(self, a, b):
                raise PresetNameConflictError(b)

            monkeypatch.setattr(type(instance.app.presets.store), "rename_preset", boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


def test_rename_compensates_and_maps_exists_on_reload_failure(pg, monkeypatch) -> None:
    cascaded: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", cascaded))

            async def reload_boom(name):
                raise PresetExistsError(name)

            monkeypatch.setattr(instance.app.preset_manager, "reload", reload_boom)
            with pytest.raises(ConflictError, match="already exists"):
                await preset_ops.rename_preset(name="old", new_name="new")
            # The store move was compensated: the preset stays live under its old name.
            assert (await instance.app.presets.store.get_preset("old")).name == "old"
            # The reload failed before the cascades' step, so no cascade ran.
            assert cascaded == []

    asyncio.run(run())


def test_rename_compensates_and_maps_name_conflict_on_reload_failure(pg, monkeypatch) -> None:
    cascaded: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", cascaded))

            async def reload_boom(name):
                raise PresetNameConflictError(name)

            monkeypatch.setattr(instance.app.preset_manager, "reload", reload_boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await preset_ops.rename_preset(name="old", new_name="new")
            assert cascaded == []

    asyncio.run(run())


# -- rename: the rename cascades and their undo --------------------------------


def test_rename_calls_every_cascade_once_per_tool_pair_after_the_new_name_binds(pg, monkeypatch) -> None:
    events: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet", extensions=[["exta"]])
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events))
            instance.app.tools.register_rename_cascade(_recording_cascade("c2", events))
            _record_rename_steps(monkeypatch, events)

            result = await preset_ops.rename_preset(name="greet", new_name="v2")

            assert result["name"] == "v2"
            # Cascade outer, tool pair inner (the preset, then its branch), between the bind of
            # the new name and the teardown of the old.
            assert events == [
                ("store", "greet", "v2"),
                ("reload", "v2"),
                ("c1", "greet", "v2"),
                ("c1", "greet_exta", "v2_exta"),
                ("c2", "greet", "v2"),
                ("c2", "greet_exta", "v2_exta"),
                ("remove", "greet"),
            ]

    asyncio.run(run())


@pytest.mark.usefixtures("admin_caller")
def test_rename_runs_no_cascade_when_a_referee_holds(pg, monkeypatch) -> None:
    events: list[Event] = []
    holder = ReferenceHolder(
        kind="schedule", tag="schedules", name="nightly", summary="schedule 'nightly' fires 'greet'"
    )

    async def _holds(names: frozenset[str]) -> dict[str, list[ReferenceHolder]]:
        return {name: [holder] for name in names if name == "greet"}

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet")
            instance.app.tools.register_rename_referee(_holds)
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events))
            _record_rename_steps(monkeypatch, events)

            with pytest.raises(ConflictError, match="cannot be renamed"):
                await preset_ops.rename_preset(name="greet", new_name="v2")

            # Neither the store nor any cascade was touched.
            assert events == []
            assert instance.app.preset_manager.is_registered("greet")

    asyncio.run(run())


def test_rename_cascade_failure_unwinds_and_maps_500(pg, monkeypatch) -> None:
    events: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet", extensions=[["exta"]])
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events, raise_on="greet_exta"))
            instance.app.tools.register_rename_cascade(_recording_cascade("c2", events))
            _record_rename_steps(monkeypatch, events)
            _record_fanout(monkeypatch, events)

            with pytest.raises(OperationFailedError) as caught:
                await preset_ops.rename_preset(name="greet", new_name="v2")

            assert caught.value.message == (
                "preset 'greet' cannot be renamed — a rename cascade failed for 'greet_exta' -> 'v2_exta': store down"
            )
            assert str(caught.value.__cause__) == "store down"
            # The applied cascade is re-keyed back, the new name unbound and the record re-pointed;
            # the old name is never torn down and nothing fans out.
            assert events == [
                ("store", "greet", "v2"),
                ("reload", "v2"),
                ("c1", "greet", "v2"),
                ("c1", "greet_exta", "v2_exta"),
                ("c1", "v2", "greet"),
                ("remove", "v2"),
                ("store", "v2", "greet"),
            ]
            mgr = instance.app.preset_manager
            assert mgr.is_registered("greet")
            assert not mgr.is_registered("v2")
            tools = await instance.app.tools.get_tools()
            assert "greet_exta" in tools
            assert "v2_exta" not in tools
            assert (await instance.app.presets.store.get_preset("greet")).name == "greet"

    asyncio.run(run())


def test_rename_cascade_undo_re_keys_every_applied_call_back_latest_first(pg, monkeypatch) -> None:
    events: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet", extensions=[["exta"]])
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events))
            instance.app.tools.register_rename_cascade(_recording_cascade("c2", events, raise_on="greet"))
            _record_rename_steps(monkeypatch, events)
            _record_fanout(monkeypatch, events)

            with pytest.raises(OperationFailedError) as caught:
                await preset_ops.rename_preset(name="greet", new_name="v2")

            assert caught.value.message == (
                "preset 'greet' cannot be renamed — a rename cascade failed for 'greet' -> 'v2': store down"
            )
            assert events == [
                ("store", "greet", "v2"),
                ("reload", "v2"),
                ("c1", "greet", "v2"),
                ("c1", "greet_exta", "v2_exta"),
                ("c2", "greet", "v2"),
                ("c1", "v2_exta", "greet_exta"),
                ("c1", "v2", "greet"),
                ("remove", "v2"),
                ("store", "v2", "greet"),
            ]
            mgr = instance.app.preset_manager
            assert mgr.is_registered("greet")
            assert not mgr.is_registered("v2")
            tools = await instance.app.tools.get_tools()
            assert "greet_exta" in tools
            assert "v2_exta" not in tools
            assert (await instance.app.presets.store.get_preset("greet")).name == "greet"

    asyncio.run(run())


def test_rename_cascade_undo_stopped_by_a_re_key_back_names_that_step(pg, monkeypatch, caplog) -> None:
    events: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet")
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events, raise_back_to="greet"))
            instance.app.tools.register_rename_cascade(_recording_cascade("c2", events, raise_on="greet"))
            _record_rename_steps(monkeypatch, events)

            with (
                caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations.presets.rename"),
                pytest.raises(OperationFailedError) as caught,
            ):
                await preset_ops.rename_preset(name="greet", new_name="v2")

            assert caught.value.message == (
                "preset 'greet' cannot be renamed — a rename cascade failed for 'greet' -> 'v2': store down"
                "; the undo stopped at re-keying 'v2' back to 'greet': store locked"
            )
            assert str(caught.value.__cause__) == "store locked"
            # The undo stops at the re-key: the new name is never unbound nor the record re-pointed.
            assert events[-1] == ("c1", "v2", "greet")
            records = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert [r.getMessage() for r in records] == [
                "rename of preset 'greet' could not be undone at re-keying 'v2' back to 'greet'"
            ]
            assert records[0].exc_info is not None

    asyncio.run(run())


def test_rename_cascade_undo_stopped_by_the_unbind_names_that_step(pg, monkeypatch, caplog) -> None:
    events: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet")
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events, raise_on="greet"))
            _record_rename_steps(monkeypatch, events, unbind_error="still serving")

            with (
                caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations.presets.rename"),
                pytest.raises(OperationFailedError) as caught,
            ):
                await preset_ops.rename_preset(name="greet", new_name="v2")

            assert caught.value.message == (
                "preset 'greet' cannot be renamed — a rename cascade failed for 'greet' -> 'v2': store down"
                "; the undo stopped at unbinding 'v2': still serving"
            )
            assert str(caught.value.__cause__) == "still serving"
            # The undo stops at the unbind: the record is never re-pointed.
            assert events[-1] == ("remove", "v2")
            records = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert [r.getMessage() for r in records] == [
                "rename of preset 'greet' could not be undone at unbinding 'v2'"
            ]
            assert records[0].exc_info is not None

    asyncio.run(run())


def test_rename_cascade_undo_failure_names_the_step(pg, monkeypatch, caplog) -> None:
    events: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet")
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events, raise_on="greet"))
            _record_rename_steps(monkeypatch, events, re_point_error="row gone")

            with (
                caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations.presets.rename"),
                pytest.raises(OperationFailedError) as caught,
            ):
                await preset_ops.rename_preset(name="greet", new_name="v2")

            assert caught.value.message == (
                "preset 'greet' cannot be renamed — a rename cascade failed for 'greet' -> 'v2': store down"
                "; the undo stopped at re-pointing the record to 'greet': row gone"
            )
            assert isinstance(caught.value.__cause__, RuntimeError)
            assert str(caught.value.__cause__) == "row gone"
            records = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert [r.getMessage() for r in records] == [
                "rename of preset 'greet' could not be undone at re-pointing the record to 'greet'"
            ]
            assert records[0].exc_info is not None

    asyncio.run(run())


def test_rename_cascade_raising_before_any_applied_unbinds_and_re_points_only(pg, monkeypatch) -> None:
    events: list[Event] = []

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("greet")
            instance.app.tools.register_rename_cascade(_recording_cascade("c1", events, raise_on="greet"))
            _record_rename_steps(monkeypatch, events)

            with pytest.raises(OperationFailedError, match="a rename cascade failed for 'greet' -> 'v2': store down"):
                await preset_ops.rename_preset(name="greet", new_name="v2")

            assert events == [
                ("store", "greet", "v2"),
                ("reload", "v2"),
                ("c1", "greet", "v2"),
                ("remove", "v2"),
                ("store", "v2", "greet"),
            ]

    asyncio.run(run())


# -- delete: conflicted hard-delete failure ----------------------------------


def test_delete_conflicted_hard_delete_failure_reraises(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A stored preset whose NAME is a live tool rehydrates into quarantine.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await instance.app.versioning.store.create("preset", "weather", body.model_dump())
            await instance.app.preset_manager.rehydrate()
            assert instance.app.preset_manager.is_quarantined("weather")

            async def del_boom(self, *a, **k):
                raise RuntimeError("hard delete kaput")

            monkeypatch.setattr(type(instance.app.versioning.store), "delete", del_boom)
            with pytest.raises(RuntimeError, match="hard delete kaput"):
                await preset_ops.delete_preset(name="weather")

    asyncio.run(run())
