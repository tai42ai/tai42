"""Oracles for the write doors' reference check over a stored kwargs blob.

Each write door hands the names it stores — its own head reference and every name the
blob composes through an agent's spec, a base's ``tool_refs`` extractor, or an inline
preset — to the one check; a miss is a 400 naming each unresolved reference, an
extractor fault a 500, and a refused write stores nothing.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp.exceptions import ToolError
from tai42_contract.manifest import ApiToolsConfig
from tai42_contract.presets import PresetBody, PresetSeed
from tai42_contract.presets.errors import PresetNotFoundError

from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.authz import execution
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    OperationFailedError,
    OperationRegistry,
    operation_metadata_of,
)
from tai42_skeleton.operations import _authority as authority
from tai42_skeleton.operations import hooks as hooks_ops
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.operations import schedules as schedules_ops
from tai42_skeleton.operations.presets.create import CreateCrossReferences, PresetSeedRefusedError
from tai42_skeleton.operations.presets.views import _create_response
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.operations.reference_graph import (
    STORED_PRESET_UNREADABLE,
    ReferenceShapeError,
    kwargs_references,
)

from .conftest import _create

_AGENT = "authorable_agent"

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [
        {
            "title": "fx",
            "module": "tests.presets._fixtures",
            "include": ["weather", "echo", "plan_tool", "plan_payload_tool", "boom_tool"],
        },
        {
            "title": "refs",
            "module": "tests.presets._ref_fixtures",
            "include": [
                "sited_refs_tool",
                "str_refs_tool",
                "keyerror_refs_tool",
                "shape_refs_tool",
                "strict_refs_tool",
            ],
        },
        {
            "title": "sched",
            "module": "tests.app._fixtures.schedule_tools",
            "include": ["backend_list_schedules", "backend_delete_schedule", "backend_export_schedules"],
        },
    ],
    "agents": [{"title": "ag", "module": "tests.routers._authoring_fixtures", "include": [_AGENT]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authority, "access_control_settings", lambda: AccessControlSettings(enable=False))
    monkeypatch.setattr(execution, "access_control_settings", lambda: AccessControlSettings(enable=False))


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> InMemoryHooksManager:
    mgr = InMemoryHooksManager(HooksSettings())
    monkeypatch.setattr(hooks_ops, "get_hooks_manager", lambda: mgr)
    return mgr


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The schedule door's dispatch, recorded instead of run: a refused create records nothing."""
    calls: list[str] = []

    async def _dispatch(dispatch_name: str, coro_factory: Any) -> Any:
        calls.append(dispatch_name)
        return {"scheduled": dispatch_name}

    async def _allow(tool_name: str, arguments: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(schedules_ops, "_dispatch_schedule_creation", _dispatch)
    monkeypatch.setattr(schedules_ops, "authorize_submitted_tool", _allow)
    return calls


async def _register_hook(tool: str, tool_kwargs: dict[str, Any]) -> dict[str, Any]:
    return await hooks_ops.register_hook(
        name="h", topic="events", tool=tool, execution_key="k-fire", tool_kwargs=tool_kwargs
    )


async def _hook_names(mgr: InMemoryHooksManager) -> list[str]:
    return sorted(await mgr.list_hooks())


async def _seed_body(name: str, base_tool: str, fixed_kwargs: dict[str, Any]) -> None:
    body = PresetBody(base_tool=base_tool, description="d", fixed_kwargs=fixed_kwargs, extensions=[])
    await instance.app.versioning.store.create("preset", name, body.model_dump())


async def _stored(name: str) -> bool:
    try:
        await instance.app.presets.store.get_preset(name)
    except PresetNotFoundError:
        return False
    return True


async def _save(name: str, fixed_kwargs: dict[str, Any]) -> dict[str, Any]:
    return await preset_ops.save_version(
        name=name,
        fixed_kwargs=fixed_kwargs,
        extensions=None,
        output_schema=None,
        output_schema_provided=False,
        description=None,
    )


def _nested_subagents(levels: int) -> dict[str, Any]:
    node: dict[str, Any] = {"tool_names": []}
    for _ in range(levels):
        node = {"subagents": [node]}
    return node


# -- a declared extractor's entries at the preset doors ----------------------------------

_TWO_MISSES = (
    "composed tool 'ghost' at step 'a' is not a registered tool\ncomposed tool 'absent' is not a registered tool"
)


def test_create_names_every_unresolved_composed_tool(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(BadRequestError) as caught:
                await _create("p", base_tool="sited_refs_tool")
            assert caught.value.message == _TWO_MISSES
            assert not await _stored("p")
            verdict = await preset_ops.validate_preset(name="p", base_tool="sited_refs_tool", description="d")
            assert verdict["valid"] is False
            assert verdict["error"] == _TWO_MISSES

    asyncio.run(run())


_PLAN_MISSES = {"plan": {"refs": ["absent"], "steps": [{"id": "a", "tool": "ghost"}]}}
_PLAN_MISS_TEXT = (
    "composed tool 'absent' is not a registered tool\ncomposed tool 'ghost' at step 'a' is not a registered tool"
)


def test_save_version_and_validate_refuse_an_unresolved_composed_tool(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p", base_tool="plan_tool", fixed_kwargs={"plan": {"refs": ["echo"]}})
            with pytest.raises(BadRequestError) as caught:
                await _save("p", _PLAN_MISSES)
            assert caught.value.message == _PLAN_MISS_TEXT
            assert [row["version"] for row in await preset_ops.list_versions(name="p")] == [1]
            verdict = await preset_ops.validate_preset(name="p", fixed_kwargs=_PLAN_MISSES)
            assert verdict == {"valid": False, "error": _PLAN_MISS_TEXT}

    asyncio.run(run())


def test_rollback_refuses_a_version_whose_composed_tool_is_gone(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p", base_tool="plan_tool", fixed_kwargs={"plan": {"refs": ["echo"]}})
            await _save("p", {"plan": {"refs": ["weather"]}})
            store = instance.app.presets.store
            real_get_version = store.get_version

            async def gone_since(self: Any, name: str, version: int) -> Any:
                row = await real_get_version(name, version)
                body = PresetBody.model_validate(row.body)
                return SimpleNamespace(body=body.model_copy(update={"fixed_kwargs": _PLAN_MISSES}).model_dump())

            with monkeypatch.context() as patched:
                patched.setattr(type(store), "get_version", gone_since)
                with pytest.raises(BadRequestError) as caught:
                    await preset_ops.rollback_preset(name="p", version=1)
            assert caught.value.message == _PLAN_MISS_TEXT
            assert (await store.get_preset("p")).active_version == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("base", "error"),
    [
        (
            "str_refs_tool",
            "tool_refs extractor of base tool 'str_refs_tool' returned a str entry, not a ComposedToolRef",
        ),
        ("keyerror_refs_tool", "tool_refs extractor of base tool 'keyerror_refs_tool' raised KeyError"),
        ("boom_tool", "tool_refs extractor of base tool 'boom_tool' raised RuntimeError"),
    ],
)
def test_a_broken_extractor_is_a_500_storing_nothing(pg, caplog, base: str, error: str) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with caplog.at_level(logging.ERROR), pytest.raises(OperationFailedError) as caught:
                await _create("p", base_tool=base)
            assert caught.value.message == error
            assert not await _stored("p")
            faults = [r for r in caplog.records if r.levelno == logging.ERROR and r.getMessage() == error]
            assert len(faults) == 1
            with pytest.raises(OperationFailedError, match=f"^{error}$"):
                await preset_ops.validate_preset(name="p", base_tool=base, description="d")

    asyncio.run(run())


def test_a_raising_extractor_logs_its_traceback(pg, caplog) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with caplog.at_level(logging.ERROR), pytest.raises(OperationFailedError):
                await _create("p", base_tool="boom_tool")
            (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert record.exc_info is not None
            assert "refs extractor kaput" in logging.Formatter().format(record)

    asyncio.run(run())


def test_an_extractor_value_error_is_a_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(BadRequestError, match=r"^plan must be a list$"):
                await _create("p", base_tool="shape_refs_tool")
            assert not await _stored("p")
            verdict = await preset_ops.validate_preset(name="p", base_tool="shape_refs_tool", description="d")
            assert verdict == {"valid": False, "error": "plan must be a list"}

    asyncio.run(run())


def test_the_write_validator_answers_before_the_extractor_is_called(pg) -> None:
    from tests.presets import _ref_fixtures

    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body: Any) -> list[str]:
                return [] if isinstance(body.fixed_kwargs.get("plan", {}), dict) else ["plan must be an object"]

            instance.app.presets.register_write_validator("strict_refs_tool", validator)
            _ref_fixtures.strict_refs_calls.clear()
            with pytest.raises(BadRequestError, match=r"^plan must be an object$"):
                await _create("p", base_tool="strict_refs_tool", fixed_kwargs={"plan": "x"})
            verdict = await preset_ops.validate_preset(
                name="p", base_tool="strict_refs_tool", description="d", fixed_kwargs={"plan": "x"}
            )
            assert verdict == {"valid": False, "error": "plan must be an object"}
            await _create("p", base_tool="strict_refs_tool", fixed_kwargs={"plan": {"refs": ["echo"]}})
            _ref_fixtures.strict_refs_calls.clear()
            with pytest.raises(BadRequestError, match=r"^plan must be an object$"):
                await _save("p", {"plan": "x"})
            assert _ref_fixtures.strict_refs_calls == []

    asyncio.run(run())


# -- an agent's spec ---------------------------------------------------------------------


def test_an_agent_preset_refuses_an_unregistered_subagent_tool(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(BadRequestError) as caught:
                await _create("p", base_tool=_AGENT, fixed_kwargs={"subagents": [{"tool_names": ["ghost"]}]})
            assert caught.value.message == "fixed_kwargs.subagents[0].tool_names entry 'ghost' is not a registered tool"
            assert not await _stored("p")

    asyncio.run(run())


def test_a_plain_tool_preset_carrying_tool_names_is_not_walked(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("plainish", "weather", {"tool_names": ["ghost"]})
            rows = {row["name"]: row for row in await preset_ops.list_presets()}
            assert rows["plainish"]["uses"] == []

    asyncio.run(run())


def test_a_malformed_agent_spec_is_a_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # The agent-authoring check reads a typed field before the reference walk does.
            with pytest.raises(BadRequestError, match=r"^fixed_kwargs field 'tool_names' is invalid for agent "):
                await _create("p", base_tool=_AGENT, fixed_kwargs={"tool_names": "x"})
            verdict = await preset_ops.validate_preset(
                name="p", base_tool=_AGENT, description="d", fixed_kwargs={"presets": [{"name": "x"}]}
            )
            assert verdict["valid"] is False
            assert verdict["error"].startswith("fixed_kwargs.presets[0] is not a valid preset spec:")
            with pytest.raises(
                BadRequestError, match=r"^at step 'a': tool_kwargs\.subagents must be a list of sub-agent specs$"
            ):
                await _create(
                    "p",
                    base_tool="plan_tool",
                    fixed_kwargs={"plan": {"steps": [{"id": "a", "tool": _AGENT, "tool_kwargs": {"subagents": "x"}}]}},
                )
            assert not await _stored("p")

    asyncio.run(run())


# -- inline presets and chained sites ----------------------------------------------------

_INLINE_GHOST = {
    "name": "x",
    "description": "d",
    "base_tool": "plan_tool",
    "fixed_kwargs": {"plan": {"steps": [{"id": "fetch", "tool": "ghost"}]}},
}


def _three_deep(tool: str) -> dict[str, Any]:
    inner = {"plan": {"steps": [{"id": "fetch", "tool": tool}]}}
    mid = {"plan": {"steps": [{"id": "mid", "tool": "plan_tool", "tool_kwargs": inner}]}}
    return {"plan": {"steps": [{"id": "outer", "tool": "plan_tool", "tool_kwargs": mid}]}}


def test_sites_chain_through_inline_presets_and_nested_steps(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(BadRequestError) as caught:
                await _create("p", base_tool=_AGENT, fixed_kwargs={"presets": [_INLINE_GHOST]})
            assert caught.value.message == (
                "composed tool 'ghost' at fixed_kwargs.presets[0].fixed_kwargs > step 'fetch' is not a registered tool"
            )
            with pytest.raises(BadRequestError) as caught:
                await _create("p", base_tool="plan_tool", fixed_kwargs=_three_deep("ghost"))
            assert caught.value.message == (
                "composed tool 'ghost' at step 'outer' > tool_kwargs > step 'mid' > tool_kwargs > step 'fetch' "
                "is not a registered tool"
            )
            assert not await _stored("p")

    asyncio.run(run())


@pytest.mark.usefixtures("admin_caller")
def test_a_stored_preset_named_deep_inside_a_body_is_held(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._rename_referee_registry.reset()
            await _create("leaf")
            inline = {**_INLINE_GHOST, "fixed_kwargs": {"plan": {"steps": [{"id": "fetch", "tool": "leaf"}]}}}
            await _create("holder_inline", base_tool=_AGENT, fixed_kwargs={"presets": [inline]})
            await _create("holder_deep", base_tool="plan_tool", fixed_kwargs=_three_deep("leaf"))
            rows = {row["name"]: row for row in await preset_ops.list_presets()}
            assert rows["leaf"]["used_by"] == ["holder_deep", "holder_inline"]
            with pytest.raises(ConflictError) as caught:
                await preset_ops.rename_preset(name="leaf", new_name="leaf2")
            holders = cast("list[dict[str, Any]]", caught.value.extra["holders"])
            assert [holder["name"] for holder in holders] == ["holder_deep", "holder_inline"]

    asyncio.run(run())


def test_a_walk_deeper_than_the_limit_is_a_400(pg, hooks) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(BadRequestError) as caught:
                await _register_hook(_AGENT, _nested_subagents(33))
            assert caught.value.message.endswith("nests more than 32 levels deep")
            assert await _hook_names(hooks) == []

    asyncio.run(run())


def test_a_self_holding_blob_is_refused_and_returns(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            blob: dict[str, Any] = {"subagents": []}
            blob["subagents"].append(blob)
            with pytest.raises(ReferenceShapeError, match=r"nests more than 32 levels deep$"):
                await kwargs_references(_AGENT, blob, "fixed_kwargs")

    asyncio.run(run())


# -- the hook and schedule doors over every target kind ----------------------------------

_AGENT_CASES = [
    ({"tool_names": ["ghost"]}, "tool_kwargs.tool_names entry 'ghost' is not a registered tool"),
    (
        {"subagents": [{"tool_names": ["ghost"]}]},
        "tool_kwargs.subagents[0].tool_names entry 'ghost' is not a registered tool",
    ),
    (
        {"presets": [{"name": "x", "description": "d", "base_tool": "ghost"}]},
        "tool_kwargs.presets[0] base_tool 'ghost' is not a registered tool",
    ),
    (
        {"presets": [_INLINE_GHOST]},
        "composed tool 'ghost' at tool_kwargs.presets[0].fixed_kwargs > step 'fetch' is not a registered tool",
    ),
    ({"tool_names": "x"}, "tool_kwargs.tool_names must be a list of tool names"),
]

_EXTRACTOR_CASES = [
    (
        {"plan": {"steps": [{"id": "fetch", "tool": "ghost"}]}},
        "composed tool 'ghost' at tool_kwargs > step 'fetch' is not a registered tool",
    ),
    (
        {"plan": {"steps": [{"id": "think", "tool": _AGENT, "tool_kwargs": {"tool_names": ["nope"]}}]}},
        "tool_kwargs.tool_names entry 'nope' at tool_kwargs > step 'think' is not a registered tool",
    ),
    ({"plan": "x"}, "at tool_kwargs: plan must be an object; got str"),
]


async def _agent_preset() -> None:
    await _create("agent_p", base_tool=_AGENT)


@pytest.mark.parametrize("target", [_AGENT, "agent_p"])
@pytest.mark.parametrize(("tool_kwargs", "error"), _AGENT_CASES)
def test_a_hook_on_an_agent_target_refuses_an_unresolved_name(pg, hooks, target, tool_kwargs, error) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _agent_preset()
            with pytest.raises(BadRequestError) as caught:
                await _register_hook(target, tool_kwargs)
            assert caught.value.message == error
            assert await _hook_names(hooks) == []

    asyncio.run(run())


@pytest.mark.parametrize("target", [_AGENT, "agent_p"])
@pytest.mark.parametrize(("tool_kwargs", "error"), _AGENT_CASES)
def test_a_schedule_on_an_agent_target_refuses_an_unresolved_name(pg, scheduled, target, tool_kwargs, error) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _agent_preset()
            with pytest.raises(BadRequestError) as caught:
                await schedules_ops.create_schedule(target, tool_kwargs)
            assert caught.value.message == error
            assert scheduled == []

    asyncio.run(run())


@pytest.mark.parametrize(("tool_kwargs", "error"), _EXTRACTOR_CASES)
def test_the_hook_and_schedule_doors_read_an_extractor_target(pg, hooks, scheduled, tool_kwargs, error) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(BadRequestError) as caught:
                await _register_hook("plan_tool", tool_kwargs)
            assert caught.value.message == error
            with pytest.raises(BadRequestError) as caught:
                await schedules_ops.create_schedule("plan_tool", tool_kwargs)
            assert caught.value.message == error
            assert await _hook_names(hooks) == []
            assert scheduled == []

    asyncio.run(run())


def test_the_hook_and_schedule_doors_refuse_an_unknown_tool(pg, hooks, scheduled) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(BadRequestError, match=r"^hook tool 'ghost' is not a registered tool$"):
                await _register_hook("ghost", {})
            with pytest.raises(BadRequestError, match=r"^scheduled tool 'ghost' is not a registered tool$"):
                await schedules_ops.create_schedule("ghost", {})
            with pytest.raises(BadRequestError) as caught:
                await schedules_ops.create_schedule(_AGENT, {"presets": [{"name": "x"}]})
            assert caught.value.message.startswith("tool_kwargs.presets[0] is not a valid preset spec:")
            assert await _hook_names(hooks) == []
            assert scheduled == []

    asyncio.run(run())


def test_a_plain_tool_target_reads_no_kwargs(pg, hooks, scheduled) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            blob = {"tool_names": ["ghost"], "presets": [{"base_tool": "ghost"}]}
            assert (await _register_hook("echo", blob))["registered"] is True
            await schedules_ops.create_schedule("echo", blob)
            assert scheduled == ["echo"]

    asyncio.run(run())


def test_an_authored_input_preset_reads_no_kwargs(pg, hooks, scheduled) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await preset_ops.create_preset(
                name="plan_inp",
                base_tool="plan_payload_tool",
                description="d",
                fixed_kwargs={},
                extensions=[],
                output_schema=None,
                input_schema={"type": "object", "properties": {"plan": {"type": "string"}}},
            )
            assert (await _register_hook("plan_inp", {"plan": "x"}))["registered"] is True
            await schedules_ops.create_schedule("plan_inp", {"plan": "x"})
            assert scheduled == ["plan_inp"]

    asyncio.run(run())


def test_a_hook_naming_a_stored_preset_inside_its_kwargs_is_no_rename_holder(pg, hooks) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._rename_referee_registry.reset()
            await _create("leaf")
            await _register_hook(_AGENT, {"tool_names": ["leaf"]})
            assert await _hook_names(hooks) == ["h"]
            renamed = await preset_ops.rename_preset(name="leaf", new_name="leaf2")
            assert renamed["name"] == "leaf2"

    asyncio.run(run())


# -- the seed applier --------------------------------------------------------------------


def test_a_seed_composing_an_unserved_tool_is_refused(pg, serve_tools) -> None:
    seed = PresetSeed(
        name="seeded", description="d", base_tool="plan_tool", fixed_kwargs={"plan": {"refs": ["absent_tool"]}}
    )

    async def run_refused() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app.presets.register_seed(seed)
            with pytest.raises(PresetSeedRefusedError) as caught:
                await preset_ops.apply_preset_seeds()
            assert str(caught.value) == (
                "preset seed 'seeded' cannot be created: preset 'seeded' over base tool 'plan_tool' — "
                "composed tool 'absent_tool' is not a registered tool"
            )
            assert not await _stored("seeded")

    async def run_served() -> None:
        async with instance.app.app_context(_manifest()):
            serve_tools("absent_tool")
            instance.app.presets.register_seed(seed)
            await preset_ops.apply_preset_seeds()
            assert await _stored("seeded")

    asyncio.run(run_refused())
    asyncio.run(run_served())


# -- a stored body the walk cannot read --------------------------------------------------

_BAD_TEXT = "stored preset 'bad': fixed_kwargs.tool_names must be a list of tool names"
_BAD_EXTRA = {"code": STORED_PRESET_UNREADABLE, "preset": "bad"}


def test_a_malformed_stored_body_fails_every_read_that_walks_it(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("ok")
            await _seed_body("bad", _AGENT, {"tool_names": "x"})
            for read in (
                preset_ops.list_presets(),
                preset_ops.get_preset(name="bad"),
                preset_ops.preset_referees(name="ok", action="rename"),
            ):
                with pytest.raises(OperationFailedError) as caught:
                    await read
                assert caught.value.message == _BAD_TEXT
                assert caught.value.extra == _BAD_EXTRA
            assert (await preset_ops.preset_referees(name="bad", action="delete"))["holders"] == []

    asyncio.run(run())


def test_the_projected_list_tool_answers_the_unreadable_row_as_a_tool_error(pg) -> None:
    class _Tools:
        def __init__(self) -> None:
            self.funcs: dict[str, Any] = {}

        def tool(self, *, force: bool, name: str, tags: Any, annotations: Any) -> Any:
            def decorator(func: Any) -> Any:
                self.funcs[name] = func
                return func

            return decorator

    projected = SimpleNamespace(tools=_Tools())
    registry = OperationRegistry()
    registry.register(operation_metadata_of(preset_ops.list_presets))
    project_operations(projected, ApiToolsConfig(), registry=registry)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("bad", _AGENT, {"tool_names": "x"})
            async with reload_gate.synchronized():
                with pytest.raises(ToolError, match=f"^{_BAD_TEXT}$"):
                    await projected.tools.funcs["list_presets"]()

    asyncio.run(run())


def test_a_stored_body_whose_extractor_raises_fails_the_listing(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("bad", "boom_tool", {})
            with pytest.raises(OperationFailedError) as caught:
                await preset_ops.list_presets()
            assert (
                caught.value.message
                == "stored preset 'bad': tool_refs extractor of base tool 'boom_tool' raised RuntimeError"
            )
            assert caught.value.extra == _BAD_EXTRA

    asyncio.run(run())


def test_a_create_beside_a_malformed_stored_body_stores_nothing(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("bad", _AGENT, {"tool_names": "x"})
            for _ in range(2):
                with pytest.raises(OperationFailedError) as caught:
                    await _create("q")
                assert caught.value.message == _BAD_TEXT
                assert caught.value.extra == _BAD_EXTRA
                assert not await _stored("q")
            with pytest.raises(OperationFailedError, match=f"^{_BAD_TEXT}$"):
                await instance.app.presets.create("q", "weather", "d", {})
            assert not await _stored("q")

            instance.app.presets.register_seed(PresetSeed(name="seeded", description="d", base_tool="weather"))
            await preset_ops.apply_preset_seeds()
            assert await _stored("seeded")

            await preset_ops.delete_preset(name="bad")
            await preset_ops.list_presets()
            created = await _create_view("q")
            assert created["name"] == "q"

    asyncio.run(run())


async def _create_view(name: str) -> dict[str, Any]:
    return await preset_ops.create_preset(
        name=name, base_tool="weather", description="d", fixed_kwargs={}, extensions=[], output_schema=None
    )


def test_the_create_response_is_built_from_the_cross_references_handed_to_it(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            view = _create_response(
                "q",
                "weather",
                "d",
                [],
                None,
                None,
                input_map=None,
                output_map=None,
                active_version=1,
                report=FleetResult(op="presets", reachable=True, results=[]),
                cross=CreateCrossReferences(uses=["a"], used_by=["b"]),
            )
            assert (view["uses"], view["used_by"]) == (["a"], ["b"])

    asyncio.run(run())
