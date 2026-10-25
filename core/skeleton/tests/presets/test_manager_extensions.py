"""Preset extension branches: a wrapper branch keeps the preset description, two
independent combos survive versioning, remove tears down base plus branches, and a
body description projects to the tool and survives reload."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator

import orjson
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_kit.monitoring.otel import OtelWriter
from tests.app._fixtures.relocate_ext import PLATFORM_STAMPS, TOOL_NAME_ARG

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring
from tai42_skeleton.tools.binding import UnknownToolError

from .._fakes.recording_monitoring import RecordingMonitoring
from ._manager_fixtures import _MANIFEST, FakeVersioningPg, _create_versioned, _live_tool_names, _manifest


def test_wrapper_branch_of_preset_keeps_preset_description(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            # ``exta`` is a ``functools.wraps`` wrapper, so its branch inherits the
            # base callable's docstring. The adoption guard must compare against the
            # BRANCH BASE callable's docstring (not the ``Tool`` object's class
            # docstring), so the wrapper is recognized as authoring no new
            # description and the preset's own description survives onto the branch.
            await _create_versioned("shouty", "echo", {}, [["exta"]], description="Preset desc")

            base = await app.tools.get_tool("shouty")
            branch = await app.tools.get_tool("shouty_exta")
            assert base.description == "Preset desc"
            assert branch.description == "Preset desc"

    asyncio.run(run())


def test_extensions_two_combos_survive_versioning(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("shouty", "echo", {}, [["exta"], ["extb"]])

            # Bare name runnable + BOTH independent branches + NO stacked branch.
            tools = set(await app.tools.get_tools())
            assert {"shouty", "shouty_exta", "shouty_extb"} <= tools
            assert "shouty_exta_extb" not in tools
            assert await app.tools.run_tool("shouty", {"text": "hi"}) == "hi"
            assert await app.tools.run_tool("shouty_exta", {"text": "hi"}) == "hi|a"
            assert await app.tools.run_tool("shouty_extb", {"text": "hi"}) == "hi|b"

            # Save a new version WITHOUT passing extensions, then reload: the new
            # active body carried base_tool + BOTH combos forward.
            await store.save_version("shouty", fixed_kwargs={})
            await mgr.reload("shouty")
            tools = set(await app.tools.get_tools())
            assert {"shouty", "shouty_exta", "shouty_extb"} <= tools
            assert "shouty_exta_extb" not in tools
            assert await app.tools.run_tool("shouty_exta", {"text": "yo"}) == "yo|a"

            # Each branch is bound EXACTLY once (the reload's teardown ran before
            # re-register — no leaked pre-reload duplicate), and the base's
            # _extend_tools holds exactly one entry per branch.
            names = _live_tool_names()
            assert names.count("shouty_exta") == 1
            assert names.count("shouty_extb") == 1
            branches = {b for b, base in app._tool_registry._extend_tools.items() if base == "shouty" and b != "shouty"}
            assert branches == {"shouty_exta", "shouty_extb"}

    asyncio.run(run())


def test_remove_tears_down_base_and_branches(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("shouty", "echo", {}, [["exta"], ["extb"]])
            assert {"shouty", "shouty_exta", "shouty_extb"} <= set(await app.tools.get_tools())

            await mgr.remove("shouty")

            tools = set(await app.tools.get_tools())
            assert not ({"shouty", "shouty_exta", "shouty_extb"} & tools)
            assert not mgr.is_registered("shouty")
            for name in ("shouty", "shouty_exta", "shouty_extb"):
                with pytest.raises(UnknownToolError):
                    await app.tools.run_tool(name, {"text": "hi"})

    asyncio.run(run())


def test_body_description_projects_to_tool_and_survives_reload(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [], description="Weather v1")
            assert (await app.tools.get_tool("wv")).description == "Weather v1"

            # A simulated reload wipes the runtime tool; rehydrate re-projects the
            # description from the persisted body.
            await mgr.remove("wv")
            await mgr.rehydrate()
            assert (await app.tools.get_tool("wv")).description == "Weather v1"

    asyncio.run(run())


# The closed input schema of a preset over ``payload_echo``: ``where`` required, ``n`` optional.
_PAYLOAD_PROPERTIES = {"where": {"type": "string", "minLength": 2}, "n": {"type": "integer", "default": 3}}
_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": _PAYLOAD_PROPERTIES,
    "required": ["where"],
    "additionalProperties": False,
}


def test_a_branch_of_a_preset_leaves_injected_parameters_to_its_transform(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            await _create_versioned("formal", "ctx_echo", {"tone": "formal"}, [["exta"]])

            branch = await app.tools.get_tool("formal_exta")
            assert set(branch.parameters["properties"]) == {"q"}
            assert await app.tools.run_tool("formal_exta", {"q": "x"}) == "x:formal:True|a"

    asyncio.run(run())


def test_a_branch_of_an_input_schema_preset_presents_the_schema_and_runs_the_preset(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            await app.preset_manager.register(
                "routed", "payload_echo", {"tag": "t1"}, [["exta"]], "d", input_schema=_PAYLOAD_SCHEMA
            )

            branch = await app.tools.get_tool("routed_exta")
            assert branch.parameters == _PAYLOAD_SCHEMA
            # The fixed kwarg applied, an omitted property's schema default filled in, and the
            # object routed into ``payload``.
            assert await app.tools.run_tool("routed_exta", {"where": "xy"}) == "xy:3:t1|a"
            assert await app.tools.run_tool("routed_exta", {"where": "xy", "n": 4}) == "xy:4:t1|a"
            with pytest.raises(Exception, match="caller input does not match the preset's input_schema"):
                await app.tools.run_tool("routed_exta", {"where": "x"})
            with pytest.raises(Exception, match="where"):
                await app.tools.run_tool("routed_exta", {})

    asyncio.run(run())


def _manifest_with(extensions_module: str) -> Manifest:
    return Manifest.model_validate(
        {**_MANIFEST, "extensions_modules": [*_MANIFEST["extensions_modules"], extensions_module]}
    )


def test_a_relocating_branch_of_an_input_schema_preset_ships_only_the_supplied_arguments(pg: FakeVersioningPg):
    async def run() -> list[dict]:
        async with app.app_context(_manifest_with("tests.app._fixtures.relocate_ext")):
            await app.preset_manager.register(
                "routed", "payload_echo", {"tag": "t1"}, [["relocate"]], "d", input_schema=_PAYLOAD_SCHEMA
            )

            assert await app.tools.run_tool("routed_relocate", {"where": "xy"}) == "xy:3:t1"
            # The manifest's extension modules are imported afresh for the context.
            return importlib.import_module("tests.app._fixtures.relocate_ext").RECORDED

    (job,) = asyncio.run(run())
    assert {key: value for key, value in job.items() if key not in PLATFORM_STAMPS} == {"where": "xy"}
    assert job[TOOL_NAME_ARG] == "routed"


@pytest.fixture
def otel_monitoring(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[OtelWriter, InMemorySpanExporter]]:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    exporter = InMemorySpanExporter()
    writer = OtelWriter(exporter=exporter)
    backend = RecordingMonitoring()
    backend._recording_writer = writer  # type: ignore[assignment]
    reset_monitoring()
    init_monitoring(backend)
    yield writer, exporter
    reset_monitoring()
    writer.shutdown()


def test_a_monitor_branch_of_an_input_schema_preset_records_only_the_supplied_arguments(
    pg: FakeVersioningPg, otel_monitoring: tuple[OtelWriter, InMemorySpanExporter]
):
    writer, exporter = otel_monitoring

    async def run():
        async with app.app_context(_manifest_with("tai42_skeleton.extensions.builtin.monitor")):
            await app.preset_manager.register(
                "routed", "payload_echo", {"tag": "t1"}, [["monitor"]], "d", input_schema=_PAYLOAD_SCHEMA
            )

            assert await app.tools.run_tool("routed_monitor", {"where": "xy"}) == "xy:3:t1"

    asyncio.run(run())
    writer.flush()
    (record,) = exporter.get_finished_spans()
    arguments = orjson.loads(str(dict(record.attributes or {})["gen_ai.tool.call.arguments"]))
    assert arguments == {"args": [], "kwargs": {"where": "xy"}}
    assert writer.export_health().records_failed == 0


def test_a_branch_of_an_input_map_preset_runs_the_map(pg: FakeVersioningPg):
    schema = {
        "type": "object",
        "properties": {"place": {"type": "string"}},
        "required": ["place"],
        "additionalProperties": False,
    }

    async def run():
        async with app.app_context(_manifest()):
            await app.preset_manager.register(
                "mapped",
                "payload_echo",
                {"tag": "t1"},
                [["exta"]],
                "d",
                input_schema=schema,
                input_map="{payload: {where: .place}}",
            )

            assert (await app.tools.get_tool("mapped_exta")).parameters == schema
            assert await app.tools.run_tool("mapped_exta", {"place": "xy"}) == "xy:None:t1|a"

    asyncio.run(run())


def test_an_open_input_schema_preset_without_extensions_binds_and_runs(pg: FakeVersioningPg):
    open_schema = {"type": "object", "properties": {"where": {"type": "string"}}}

    async def run():
        async with app.app_context(_manifest()):
            await app.preset_manager.register("open", "payload_echo", {"tag": "t2"}, [], "d", input_schema=open_schema)

            assert await app.tools.run_tool("open", {"where": "xy", "extra": 1}) == "xy:None:t2"

    asyncio.run(run())
