"""Oracles for the preset reference graph — the builtin ``tool_names`` walk, the
uses / used_by cross-reference maps, and the declared ``tool_refs`` extractor union."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.presets import PresetBody

from tai42_skeleton.app import instance
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations import presets as preset_ops

from .conftest import _create, _manifest


def test_referenced_tool_names_builtin_walk() -> None:
    assert preset_ops._referenced_tool_names({"tool_names": ["other"]}) == {"other"}
    assert preset_ops._referenced_tool_names({"subagents": [{"tool_names": ["x"]}]}) == {"x"}
    assert preset_ops._referenced_tool_names({"tool_names": ["other"]}) & {"target"} == set()


# -- uses / used_by cross-references -----------------------------------------


async def _seed_body(name: str, base_tool: str, fixed_kwargs: dict[str, Any]) -> None:
    # Seed an active body through the GENERIC store (like the referees oracle) so a
    # composing body can name another preset without that preset being a live tool at
    # author time.
    body = PresetBody(base_tool=base_tool, description="d", fixed_kwargs=fixed_kwargs, extensions=[])
    await instance.app.versioning.store.create("preset", name, body.model_dump())


def test_uses_used_by_reference_chain(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})
            await _seed_body("lonely", "weather", {"units": "x"})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["composer"]["uses"] == ["leaf"]
            assert rows["composer"]["used_by"] == []
            assert rows["leaf"]["uses"] == []
            assert rows["leaf"]["used_by"] == ["composer"]
            assert rows["lonely"]["uses"] == []
            assert rows["lonely"]["used_by"] == []

    asyncio.run(run())


def test_presets_facet_used_by_seam(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})

            assert await instance.app.presets.used_by("leaf") == ["composer"]
            assert await instance.app.presets.used_by("composer") == []

            from tai42_contract.presets.errors import PresetNotFoundError

            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.used_by("no-such-preset")

    asyncio.run(run())


def test_tools_facet_declared_extras_direct_and_inherited(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A base tool's declared ``extras_keys`` are read directly by name.
            assert await instance.app.tools.declared_extras("plan_tool") == frozenset({"warm_start"})
            # A tool that declared none reads an empty set, and an unknown name too.
            assert await instance.app.tools.declared_extras("weather") == frozenset()
            assert await instance.app.tools.declared_extras("no-such-tool") == frozenset()

            # A live preset over the base tool INHERITS the base's declared keys via the
            # parent-tool chain walk.
            await _create("planner", base_tool="plan_tool", fixed_kwargs={"plan": {"refs": []}})
            assert await instance.app.tools.declared_extras("planner") == frozenset({"warm_start"})

    asyncio.run(run())


def test_uses_excludes_self_and_non_preset_base_tools(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # ``selfref`` names itself and the base tool ``weather`` (a registered
            # non-preset tool). Self is never listed and a non-preset tool is not in
            # the population, so ``uses`` is empty.
            await _seed_body("selfref", "echo", {"tool_names": ["selfref", "weather"]})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["selfref"]["uses"] == []
            assert rows["selfref"]["used_by"] == []

    asyncio.run(run())


def test_get_row_carries_same_references_as_list_row(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})

            list_row = next(r for r in await preset_ops.list_presets() if r["name"] == "composer")
            get_row = await preset_ops.get_preset(name="composer")
            assert get_row["uses"] == list_row["uses"] == ["leaf"]
            assert get_row["used_by"] == list_row["used_by"] == []

    asyncio.run(run())


def test_used_by_matches_referees_op(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})

            leaf_row = next(r for r in await preset_ops.list_presets() if r["name"] == "leaf")
            referees = (await preset_ops.preset_referees(name="leaf"))["referees"]
            assert leaf_row["used_by"] == referees == ["composer"]

    asyncio.run(run())


# -- declared tool references (a base tool's tool_refs extractor) -------------

# A base tool declares a ``tool_refs`` extractor at registration; the reference
# collector unions the names it reads out of a preset's ``fixed_kwargs`` with the
# builtin ``tool_names`` walk, so a DECLARED reference counts in uses/used_by,
# referees and the rename guard. The fixture ``plan_tool`` reads ``plan.refs``; a
# base tool WITHOUT a declaration keeps exactly the builtin walk.


def test_declared_refs_feed_uses_and_used_by(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("comp", "plan_tool", {"plan": {"refs": ["leaf"]}})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["comp"]["uses"] == ["leaf"]
            assert rows["comp"]["used_by"] == []
            assert rows["leaf"]["uses"] == []
            assert rows["leaf"]["used_by"] == ["comp"]

    asyncio.run(run())


def test_declared_referee_blocks_rename_and_lists_in_referees(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("leaf", base_tool="weather", fixed_kwargs={"units": "v"})
            await preset_ops.create_preset(
                name="comp",
                base_tool="plan_tool",
                description="d",
                fixed_kwargs={"plan": {"refs": ["leaf"]}},
                extensions=[],
                output_schema=None,
            )

            referees = (await preset_ops.preset_referees(name="leaf"))["referees"]
            assert referees == ["comp"]
            # The rename guard now sees the declared reference too.
            with pytest.raises(ConflictError, match="cannot be renamed"):
                await preset_ops.rename_preset(name="leaf", new_name="leaf2")

    asyncio.run(run())


def test_create_response_carries_declared_refs(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("leaf", base_tool="weather", fixed_kwargs={"units": "v"})
            created = await preset_ops.create_preset(
                name="comp",
                base_tool="plan_tool",
                description="d",
                fixed_kwargs={"plan": {"refs": ["leaf"]}},
                extensions=[],
                output_schema=None,
            )
            assert created["uses"] == ["leaf"]
            assert created["used_by"] == []

    asyncio.run(run())


def test_no_declaration_ignores_nested_config(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # ``weather`` declares no extractor, so its nested config is NOT read — only
            # the builtin ``tool_names`` walk applies (empty here), unchanged behavior.
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("plainish", "weather", {"plan": {"refs": ["leaf"]}, "units": "z"})

            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["plainish"]["uses"] == []
            assert rows["leaf"]["used_by"] == []

    asyncio.run(run())


def test_declared_refs_extractor_raising_propagates(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("boom", "boom_tool", {})
            # The extractor raises; the op surfaces it loudly, never an empty-list fallback.
            with pytest.raises(RuntimeError, match="refs extractor kaput"):
                await preset_ops.list_presets()

    asyncio.run(run())


def test_declared_refs_non_string_entry_raises(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _seed_body("comp", "plan_tool", {"plan": {"refs": [123]}})
            # A declaration returning garbage is a plugin bug, raised loudly.
            with pytest.raises(TypeError, match="non-string reference"):
                await preset_ops.list_presets()

    asyncio.run(run())
