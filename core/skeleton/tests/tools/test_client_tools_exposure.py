"""``get_client_tools`` exposure of injected-param and preset tools.

An injected fastmcp ``Context`` param must NOT reach the advertised handle schema (the
model would be asked to supply it), and the handle must still run through the in-process
``bridge_context`` so ``ctx.sample()`` falls back to the platform LLM — exactly as
``run_tool`` does. A preset (a ``TransformedTool``) is exposed too and its handle runs the
preset's own transformed tool: its fixed values, input routing, input and output maps and
output schema hold as on every other door. An extension branch over a preset runs the preset.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastmcp.server.context import Context
from tai42_contract.secrets import SecretValue
from tai42_contract.tools import ClientToolError

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools import sampling_bridge
from tai42_skeleton.tools.binding.surface import remove_surface_tool


class _FakeModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content=self.reply)


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(app._fast_mcp, tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def test_client_tool_strips_injected_ctx_and_runs_via_bridge(monkeypatch, caplog):
    monkeypatch.setattr(sampling_bridge, "platform_llm", lambda: _FakeModel("42 apples"))

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def summarize(q: str, ctx: Context) -> str:
                """Summarize a query by asking the caller's LLM to sample."""
                res = await ctx.sample(q)
                assert res.text is not None
                return f"{q}:{res.text}"

            [client_tool] = await app.tools.get_client_tools(["summarize"])

            # The advertised schema INCLUDES the real user param and EXCLUDES the
            # injected fastmcp Context — the model is never asked to supply ``ctx``.
            assert "q" in client_tool.input_schema["properties"]
            assert "ctx" not in client_tool.input_schema["properties"]

            # And the tool still executes: the in-process bridge resolves
            # ``ctx.sample()`` to the platform LLM fallback.
            with caplog.at_level(logging.INFO):
                out = await client_tool.invoke({"q": "How many apples?"})
            assert out == "How many apples?:42 apples"
            assert any("falling back to the platform LLM" in r.message for r in caplog.records)

    asyncio.run(run())


def test_client_tool_masks_wrapped_secrets_in_the_result():
    # The handle feeds an agent engine (the model, the checkpoint, the callback trace), so a
    # wrapped secret in the result is masked to the placeholder — the model never sees the real
    # value. This is distinct from the sync run-tool and MCP doors, which REVEAL to the live caller.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def vault() -> dict:
                """Return a dict carrying a wrapped secret."""
                return {"token": SecretValue("tok-4242-xyzzy")}

            [client_tool] = await app.tools.get_client_tools(["vault"])
            out = await client_tool.invoke({})

            assert out == {"token": "[secret]"}
            assert "tok-4242-xyzzy" not in repr(out)

    asyncio.run(run())


def test_client_tool_exposes_preset_and_bakes_constant(preset_manager_restored):
    async def run() -> None:
        manifest = Manifest.model_validate(
            {"tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather"]}]}
        )
        async with app.app_context(manifest):
            await app.preset_manager.register("paris", "weather", {"units": "imperial"}, [], "Paris weather")

            # A preset is a TransformedTool, not a FunctionTool — it is still
            # exposed, by name and in the full set.
            names = {t.name for t in await app.tools.get_client_tools()}
            assert "paris" in names
            [client_tool] = await app.tools.get_client_tools(["paris"])

            # The baked key is hidden from the advertised schema; the remaining
            # user arg keeps its real type.
            assert "units" not in client_tool.input_schema["properties"]
            assert "city" in client_tool.input_schema["properties"]

            # Invoking serves the baked constant...
            out = await client_tool.invoke({"city": "paris"})
            assert out == {"city": "paris", "units": "imperial"}

            # ...and the baked key can never override it: the preset's transform refuses it,
            # a model-visible refusal.
            with pytest.raises(
                ClientToolError, match=r"^Error calling tool 'paris': Got unexpected keyword argument\(s\): units"
            ):
                await client_tool.invoke({"city": "paris", "units": "metric"})

    asyncio.run(run())


_PRESET_TOOLS = ["weather", "payload_echo", "failing_connector"]


def _preset_manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "extensions_modules": ["tests.presets._ext_fixtures"],
            "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": _PRESET_TOOLS}],
        }
    )


_STRICT_OUTPUT = {"type": "object", "properties": {"temp": {"type": "number"}}, "required": ["temp"]}
_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {"where": {"type": "string", "minLength": 2}, "n": {"type": "integer", "default": 3}},
    "required": ["where"],
    "additionalProperties": False,
}


async def _handle(name: str) -> Any:
    [client_tool] = await app.tools.get_client_tools([name])
    return client_tool


def test_a_preset_handle_enforces_the_presets_output_schema(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_preset_manifest()):
            await app.preset_manager.register("strict", "weather", {}, [], "d", output_schema=_STRICT_OUTPUT)

            with pytest.raises(
                ClientToolError,
                match=r"^Error calling tool 'strict': preset 'strict' output does not match its output_schema: ",
            ):
                await (await _handle("strict")).invoke({"city": "x"})

    asyncio.run(run())


def test_an_input_schema_preset_handle_routes_the_object_with_its_fixed_values(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_preset_manifest()):
            await app.preset_manager.register(
                "routed", "payload_echo", {"tag": "t9"}, [], "d", input_schema=_PAYLOAD_SCHEMA
            )

            handle = await _handle("routed")
            assert handle.input_schema == _PAYLOAD_SCHEMA
            assert await handle.invoke({"where": "xy", "n": 2}) == "xy:2:t9"

    asyncio.run(run())


def test_an_output_map_preset_handle_returns_the_mapped_value(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_preset_manifest()):
            await app.preset_manager.register("city_only", "weather", {}, [], "d", output_map=".city")

            assert await (await _handle("city_only")).invoke({"city": "x"}) == "x"

    asyncio.run(run())


def test_a_preset_handle_over_a_failed_connector_returns_the_error_envelope(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_preset_manifest()):
            await app.preset_manager.register("fc", "failing_connector", {"query": "q"}, [], "d")

            value = await (await _handle("fc")).invoke({})
            assert value["isError"] is True
            assert value["content"][0]["text"] == "hub-err:upstream down for q"

    asyncio.run(run())


def test_a_wrapper_branch_of_a_preset_enforces_and_advertises_its_output_schema(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_preset_manifest()):
            await app.preset_manager.register("strict", "weather", {}, [["exta"]], "d", output_schema=_STRICT_OUTPUT)

            assert (await app.tools.get_tool("strict_exta")).output_schema == _STRICT_OUTPUT
            with pytest.raises(
                ClientToolError,
                match=r"^Error calling tool 'strict_exta': preset 'strict' output does not match its output_schema: ",
            ):
                await (await _handle("strict_exta")).invoke({"city": "x"})

    asyncio.run(run())


def test_the_handles_of_an_input_schema_preset_and_its_branch_run_the_preset(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_preset_manifest()):
            await app.preset_manager.register(
                "routed", "payload_echo", {"tag": "t1"}, [["exta"]], "d", input_schema=_PAYLOAD_SCHEMA
            )

            assert await (await _handle("routed")).invoke({"where": "xy"}) == "xy:3:t1"
            branch = await _handle("routed_exta")
            assert await branch.invoke({"where": "xy"}) == "xy:3:t1|a"
            with pytest.raises(
                ClientToolError,
                match=r"^Error calling tool 'routed_exta': .*missing 1 required keyword-only argument: 'where'",
            ):
                await branch.invoke({})

    asyncio.run(run())
