"""The synthesized agent ``run`` tool: schema advertisement, set-fields-only
forwarding, extension composition, and the thread-namespace seams."""

from __future__ import annotations

import asyncio
import copy
import warnings
from typing import cast

import pytest
from fastmcp.tools.base import Tool
from fastmcp.tools.function_tool import FunctionTool
from pydantic.json_schema import PydanticJsonSchemaWarning
from tai42_contract.interactions import SuspendedInteraction

from tai42_skeleton.agent.session_thread import agent_session_thread
from tai42_skeleton.agent.thread_reservation import ReservedThreadNamespaceError
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.plugins.quarantine import quarantined_plugins
from tai42_skeleton.tools.binding.schema import _derive_input_schema

from .conftest import _fixture_flag


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["echo_fields"]}]}
    )


def _nested_manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "agents": [
                {
                    "title": "agents",
                    "module": "tests.agent._fixtures",
                    "include": ["echo_fields", "nested_fields"],
                }
            ]
        }
    )


def _cache_manifest(*agents: str) -> Manifest:
    # Attach the real toolbox ``cache`` WRAPPER to each agent name; ``shout`` is
    # only present because a manifest ``tools:`` entry needs a selected tool.
    return Manifest.model_validate(
        {
            "extensions_modules": ["tai42_toolbox.extensions.cache"],
            "tools": [
                {
                    "title": "fxt",
                    "module": "tests.app._fixtures.tools_b",
                    "include": ["shout"],
                    "extensions": {agent: [["cache"]] for agent in agents},
                }
            ],
            "agents": [
                {"title": "agents", "module": "tests.agent._fixtures", "include": list(agents)},
            ],
        }
    )


def _derive_under_ignore(tool: Tool) -> dict:
    # An agent run tool's concrete signature carries the ``_UNSET`` sentinel as the
    # default of every optional parameter; deriving its JSON schema deliberately
    # excludes that non-serializable default (a benign PydanticJsonSchemaWarning).
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=PydanticJsonSchemaWarning)
        return _derive_input_schema(cast(FunctionTool, tool).fn)


def test_agent_meta_threads_onto_the_run_tool():
    async def run() -> None:
        manifest = Manifest.model_validate(
            {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["meta_carrier"]}]}
        )
        async with app.app_context(manifest):
            tool = await app.tools.get_tool("meta_carrier")
            # The generic registration meta rode the ``FunctionTool`` constructor (the ONE
            # effective place for a prebuilt tool), naming no consumer concept.
            assert (tool.meta or {}).get("tai42/crash_resume") is True

    asyncio.run(run())


def test_run_tool_advertises_typed_schema_and_forwards_only_set_fields():
    async def run() -> None:
        async with app.app_context(_manifest()):
            # list_tools advertises the ToolInput fields — NOT an empty **kwargs
            # schema (which would advertise no fields at all).
            tool = await app.tools.get_tool("echo_fields")
            assert set(tool.parameters.get("properties", {})) == {"text", "times", "note"}
            assert set(tool.parameters.get("required", [])) == {"text"}

            # Only the caller-supplied field is forwarded to run; the omitted
            # defaults (``times``, ``note``) are NOT — set-fields-only.
            assert await app.tools.run_tool("echo_fields", {"text": "hi"}) == "text"
            assert await app.tools.run_tool("echo_fields", {"text": "hi", "times": 2}) == "text,times"

    asyncio.run(run())


def test_explicit_none_is_unset_for_a_null_default_field_and_kept_otherwise():
    async def run() -> None:
        manifest = Manifest.model_validate(
            {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["null_default_fields"]}]}
        )
        async with app.app_context(manifest):
            # ``optional_null`` advertises a ``null`` default: an explicit ``None`` is
            # indistinguishable from omission and dropped, so it is absent from the
            # forwarded set fields (``model_fields_set``).
            assert await app.tools.run_tool("null_default_fields", {"text": "hi", "optional_null": None}) == "text"
            # ``optional_valued`` advertises a non-``null`` default: an explicit ``None`` is a
            # real value and stays in the set fields.
            assert (
                await app.tools.run_tool("null_default_fields", {"text": "hi", "optional_valued": None})
                == "optional_valued,text"
            )
            # An omitted optional field arrives as the ``_UNSET`` sentinel and is dropped.
            assert await app.tools.run_tool("null_default_fields", {"text": "hi"}) == "text"

    asyncio.run(run())


def test_transformed_tool_shape_drops_every_null_default_none_at_once():
    async def run() -> None:
        manifest = Manifest.model_validate(
            {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["null_default_fields"]}]}
        )
        async with app.app_context(manifest):
            # A transformed tool (a preset) fills every omitted optional argument with its
            # schema default before forwarding, so the run tool receives an explicit ``None``
            # for each null-default field. All such ``None``s are dropped together; a
            # non-null-default field given an explicit ``None`` stays as a real value.
            assert (
                await app.tools.run_tool(
                    "null_default_fields", {"text": "hi", "optional_null": None, "optional_valued": None}
                )
                == "optional_valued,text"
            )

    asyncio.run(run())


def test_client_tool_advertises_agent_tool_input_schema_and_runs():
    async def run() -> None:
        async with app.app_context(_manifest()):
            [client_tool] = await app.tools.get_client_tools(["echo_fields"])

            # Surfaced as a client subtool, the agent run tool advertises its real
            # ToolInput FIELDS via its explicit ``.parameters`` (the concrete
            # signature carries sentinel defaults langchain cannot round-trip).
            assert set(client_tool.args) == {"text", "times", "note"}

            # And it still executes end-to-end through the client tool: the
            # permissive runnable forwards set-fields only (``times`` omitted → not
            # forwarded), so the body reports only ``text``.
            assert await client_tool.ainvoke({"text": "hi"}) == "text"

    asyncio.run(run())


def test_agent_run_tool_returns_suspended_interaction_when_the_run_parks():
    # Producer sentinel: a park crossing the agent TOOL-face is the SuspendedInteraction
    # sentinel (recognized by TYPE), never the internal suspended-receipt dict the agent's own
    # driver consumes — so a caller running the agent as a tool recognizes the park uniformly.

    async def run() -> None:
        manifest = Manifest.model_validate(
            {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["parking"]}]}
        )
        async with app.app_context(manifest):
            result = await app.tools.run_tool("parking", {"text": "hi"})
            assert isinstance(result, SuspendedInteraction)
            assert result.interaction_id == "i-parked"
            # It names NO resume owner: the parked run holds its own resume state against that
            # interaction, so no caller may adopt the park as its own (``assert_park_adoptable``).
            assert result.resume_owner is None

    asyncio.run(run())


def test_set_fields_only_preserved_for_scalar_nested_and_unset_fields():
    async def run() -> None:
        async with app.app_context(_nested_manifest()):
            # Scalar-only subset.
            assert await app.tools.run_tool("nested_fields", {"text": "hi"}) == "text"
            # A nested-model field set alongside a scalar forwards exactly that
            # subset; the unset scalar/nested fields (``times``, ``subagents``,
            # ``inline_skills``) are NOT forwarded.
            forwarded = await app.tools.run_tool("nested_fields", {"text": "hi", "presets": [{"base_tool": "x"}]})
            assert forwarded == "presets,text"

    asyncio.run(run())


def test_agent_base_tool_advertises_exact_model_schema():
    async def run() -> None:
        async with app.app_context(_nested_manifest()):
            from ._fixtures import EchoInput, NestedInput

            echo = await app.tools.get_tool("echo_fields")
            nested = await app.tools.get_tool("nested_fields")
            # The base tool's advertised input schema DEEP-EQUALS the model schema
            # — exact by construction (set out-of-band on ``parameters``).
            assert echo.parameters == EchoInput.model_json_schema()
            assert nested.parameters == NestedInput.model_json_schema()

    asyncio.run(run())


def test_branch_schema_equals_base_plus_extension_params():
    async def run() -> None:
        async with app.app_context(_cache_manifest("echo_fields")):
            from ._fixtures import EchoInput

            base = await app.tools.get_tool("echo_fields")
            branch = await app.tools.get_tool("echo_fields_cache")

            base_derived = _derive_under_ignore(base)
            branch_derived = _derive_under_ignore(branch)

            # The branch derives base params PLUS the wrapper's own ``exp`` control.
            assert set(branch_derived["properties"]) == set(base_derived["properties"]) | {"exp"}

            # Branch MINUS the extension's param deep-equals the base's derived
            # schema — the branch carries the agent's exact input contract.
            without_exp = copy.deepcopy(branch_derived)
            without_exp["properties"].pop("exp")
            if "exp" in without_exp.get("required", []):
                without_exp["required"].remove("exp")
            assert without_exp == base_derived

            # The ONE documented allowance: the derived schema omits the ``default``
            # key the model schema carries for optional fields (the sentinel
            # suppresses it) — and NO agent field gains a garbage default. The only
            # property carrying a default is the wrapper's own ``exp``.
            model_schema = EchoInput.model_json_schema()
            assert "default" in model_schema["properties"]["times"]
            assert "default" not in base_derived["properties"]["times"]
            with_defaults = {name for name, prop in branch_derived["properties"].items() if "default" in prop}
            assert with_defaults == {"exp"}

    asyncio.run(run())


def test_nested_models_survive_as_defs_on_a_branch():
    async def run() -> None:
        async with app.app_context(_cache_manifest("nested_fields")):
            branch = await app.tools.get_tool("nested_fields_cache")
            derived = _derive_under_ignore(branch)

            # The nested pydantic-model fields survive as ``$defs`` refs through the
            # branch composition.
            assert set(derived.get("$defs", {})) == {"PresetSpecLike", "SubAgentSpecLike", "InlineSkillLike"}
            assert derived["properties"]["presets"]["anyOf"][0]["items"]["$ref"] == "#/$defs/PresetSpecLike"
            assert derived["properties"]["subagents"]["anyOf"][0]["items"]["$ref"] == "#/$defs/SubAgentSpecLike"
            assert derived["properties"]["inline_skills"]["anyOf"][0]["items"]["$ref"] == "#/$defs/InlineSkillLike"

    asyncio.run(run())


def test_agent_registration_rejects_stray_preset_bakeable_field():
    # A ``preset_bakeable_fields`` entry that is not a real ``ToolInput`` field could
    # never pass the preset route's unknown-field check, so registration rejects it
    # loudly (naming the stray field). The rejection quarantines the agents module
    # (an additive plugin never aborts boot): the agent stays unregistered and the
    # stray field is named in the quarantine reason.
    manifest = Manifest.model_validate(
        {
            "agents": [
                {
                    "title": "bad",
                    "module": "tests.agent._bad_bakeable_fixtures",
                    "include": ["bad_bakeable_agent"],
                }
            ]
        }
    )

    async def run() -> None:
        async with app.app_context(manifest):
            assert "ghost_field" in quarantined_plugins()["tests.agent._bad_bakeable_fixtures"]
            assert "bad_bakeable_agent" not in await app.tools.get_tools()

    asyncio.run(run())


def test_agent_run_tool_accepts_extension_combo():
    # A manifest extension combo targeting an agent name now binds a branch composed
    # over the run tool's concrete signature: the base is untouched (advertising its
    # exact ToolInput schema) and the branch runs the agent behind the wrapper.
    manifest = Manifest.model_validate(
        {
            "extensions_modules": ["tests.app._fixtures.ext_mod"],
            "tools": [
                {
                    "title": "fxt",
                    "module": "tests.app._fixtures.tools_b",
                    "include": ["shout"],
                    "extensions": {"echo_fields": [["loud"]]},
                }
            ],
            "agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["echo_fields"]}],
        }
    )

    async def run() -> None:
        async with app.app_context(manifest):
            from ._fixtures import EchoInput

            tools = await app.tools.get_tools()
            assert {"echo_fields", "echo_fields_loud"} <= set(tools)

            # Base still advertises the exact model schema; the branch composed a
            # real signature (not an open bag).
            base = await app.tools.get_tool("echo_fields")
            assert base.parameters == EchoInput.model_json_schema()

            # Both run: the base forwards set-fields-only, and the branch runs the
            # agent behind the ``loud`` wrapper (whose sync body passes the agent's
            # awaited result through). End-to-end transformer/wrapper execution over
            # an agent is exercised by the cache+chain test below.
            assert await app.tools.run_tool("echo_fields", {"text": "hi"}) == "text"
            assert await app.tools.run_tool("echo_fields_loud", {"text": "hi"}) == "text"

    asyncio.run(run())


def test_cache_and_chain_compose_over_agent_run_tool_across_a_real_run():
    # A WRAPPER (cache) and a TRANSFORMER (chain) compose over the same agent run
    # tool and both execute end-to-end.
    manifest = Manifest.model_validate(
        {
            "extensions_modules": ["tai42_toolbox.extensions.cache", "tai42_toolbox.extensions.chain"],
            "tools": [
                {
                    "title": "fxt",
                    "module": "tests.app._fixtures.tools_b",
                    "include": ["shout"],
                    "extensions": {"echo_fields": [["cache"], ["chain"]]},
                }
            ],
            "agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["echo_fields"]}],
        }
    )

    async def run() -> None:
        async with app.app_context(manifest):
            from ._fixtures import EchoInput

            tools = await app.tools.get_tools()
            assert {"echo_fields", "echo_fields_cache", "echo_fields_chain"} <= set(tools)

            # cache (WRAPPER): runs the real agent behind the memoizing branch,
            # preserving set-fields-only; a repeat call serves the memoized value.
            assert await app.tools.run_tool("echo_fields_cache", {"text": "hi"}) == "text"
            assert await app.tools.run_tool("echo_fields_cache", {"text": "hi"}) == "text"

            # chain (TRANSFORMER): runs the agent, jq-transforms its output into
            # args for ``shout``, and returns that tool's result.
            chained = await app.tools.run_tool(
                "echo_fields_chain",
                {
                    "text": "hi",
                    "times": 2,
                    "note": "n",
                    "jq_expression": {"content": "{text: .}"},
                    "next_tool_name": "shout",
                },
            )
            assert chained == "note,text,times"

            # The base's advertised schema is untouched by the composition — it
            # still byte-equals the model schema (the A↔B advertise seam).
            base = await app.tools.get_tool("echo_fields")
            assert base.parameters == EchoInput.model_json_schema()

    asyncio.run(run())


def test_chain_over_agent_run_tool_omitting_an_optional_preserves_set_fields_only():
    # A chain TRANSFORMER re-dispatches the agent run tool BY NAME; an optional the
    # caller omits must not reach the agent as the _UNSET sentinel (which would fail
    # validation) — the run_tool boundary strips it, so the agent sees only ``text``.
    manifest = Manifest.model_validate(
        {
            "extensions_modules": ["tai42_toolbox.extensions.chain"],
            "tools": [
                {
                    "title": "fxt",
                    "module": "tests.app._fixtures.tools_b",
                    "include": ["shout"],
                    "extensions": {"echo_fields": [["chain"]]},
                }
            ],
            "agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["echo_fields"]}],
        }
    )

    async def run() -> None:
        async with app.app_context(manifest):
            chained = await app.tools.run_tool(
                "echo_fields_chain",
                {"text": "hi", "jq_expression": {"content": "{text: .}"}, "next_tool_name": "shout"},
            )
            # Only ``text`` was supplied, so the agent echoed only ``text`` (no sentinel
            # for the omitted ``times``/``note``), and ``shout`` returned it unchanged.
            assert chained == "text"

    asyncio.run(run())


# -- the reserved bridge: thread namespace ------------------------------------


def _config_manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["config_fields"]}]}
    )


def test_the_run_tool_refuses_a_reserved_bridge_thread():
    # The HTTP run doors are not the only way to reach an agent: the auto-registered run
    # tool is a second door onto the same seam, and the reservation holds there too.
    async def run() -> None:
        async with app.app_context(_config_manifest()):
            with pytest.raises(ReservedThreadNamespaceError, match="thread_id"):
                await app.tools.run_tool(
                    "config_fields",
                    {"text": "hi", "langgraph_config": {"configurable": {"thread_id": "bridge:chat:+15550001111"}}},
                )
            with pytest.raises(ReservedThreadNamespaceError, match="checkpoint_id"):
                await app.tools.run_tool(
                    "config_fields",
                    {"text": "hi", "langgraph_config": {"configurable": {"checkpoint_id": "bridge:chat:x"}}},
                )

    asyncio.run(run())


def test_the_run_tool_allows_an_unreserved_thread():
    async def run() -> None:
        async with app.app_context(_config_manifest()):
            answer = await app.tools.run_tool(
                "config_fields",
                {"text": "hi", "langgraph_config": {"configurable": {"thread_id": "user-42"}}},
            )
            assert answer == "user-42"

    asyncio.run(run())


# -- the ambient in-process session thread ------------------------------------


def _thread_recorder_manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["thread_recorder"]}]}
    )


def _thread_recorder_seen() -> list[str | None]:
    # The fixtures module is re-imported on each app_context enter, so read the thread log
    # off the LIVE module object rather than a stale import binding.
    return _fixture_flag("tests.agent._fixtures", "thread_recorder_seen")


def test_run_tool_injects_the_ambient_session_thread_when_caller_pins_none():
    # A driver deposits an ambient session thread (naming no consumer) and the caller pins
    # no thread of its own: the synthesized run tool threads the deposit onto the run, so
    # the agent runs under the deposited id.
    async def run() -> None:
        async with app.app_context(_thread_recorder_manifest()):
            _thread_recorder_seen().clear()
            with agent_session_thread("flow:run-1:node-a"):
                answer = await app.tools.run_tool("thread_recorder", {"text": "hi"})
            assert answer == "flow:run-1:node-a"
            assert _thread_recorder_seen() == ["flow:run-1:node-a"]

    asyncio.run(run())


def test_run_tool_injects_nothing_without_a_deposit_fresh():
    # No deposit: the contextvar stays None, the run tool injects no thread_id, and the run
    # is byte-identical to the pre-deposit behavior (the agent mints its own fresh thread).
    async def run() -> None:
        async with app.app_context(_thread_recorder_manifest()):
            _thread_recorder_seen().clear()
            answer = await app.tools.run_tool("thread_recorder", {"text": "hi"})
            assert answer == ""
            assert _thread_recorder_seen() == [None]

    asyncio.run(run())


def test_a_caller_pinned_thread_wins_over_the_ambient_deposit():
    # The deposit only fills an ABSENT thread_id: a caller that pinned its own thread keeps
    # it, so the deposit never overrides an explicit choice.
    async def run() -> None:
        async with app.app_context(_thread_recorder_manifest()):
            _thread_recorder_seen().clear()
            with agent_session_thread("flow:run-1:node-a"):
                answer = await app.tools.run_tool("thread_recorder", {"text": "hi", "thread_id": "caller-pinned"})
            assert answer == "caller-pinned"
            assert _thread_recorder_seen() == ["caller-pinned"]

    asyncio.run(run())


def test_a_config_shaped_caller_pin_also_wins_over_the_ambient_deposit():
    # The caller-pinned check covers the config-shaped spelling too: a
    # ``configurable.thread_id`` on a config-bearing kwarg means the deposit must NOT
    # inject a top-level ``thread_id`` (which build_run_config would let override the
    # caller's config-shaped pin). The recorder sees no top-level thread at all — the
    # caller's config rides through untouched.
    async def run() -> None:
        async with app.app_context(_thread_recorder_manifest()):
            _thread_recorder_seen().clear()
            with agent_session_thread("flow:run-1:node-a"):
                answer = await app.tools.run_tool(
                    "thread_recorder",
                    {"text": "hi", "langgraph_config": {"configurable": {"thread_id": "cfg-pinned"}}},
                )
            assert answer == ""
            assert _thread_recorder_seen() == [None]

    asyncio.run(run())


def test_a_reserved_bridge_deposit_is_still_refused():
    # The deposit is a caller-external steering vector like any thread id, so it passes the
    # SAME reserved-namespace guard: a ``bridge:`` deposit is refused however it arrives.
    async def run() -> None:
        async with app.app_context(_thread_recorder_manifest()):
            _thread_recorder_seen().clear()
            with (
                agent_session_thread("bridge:chat:+15550001111"),
                pytest.raises(ReservedThreadNamespaceError, match="thread_id"),
            ):
                await app.tools.run_tool("thread_recorder", {"text": "hi"})
            # The run was refused before reaching the agent — nothing was recorded.
            assert _thread_recorder_seen() == []

    asyncio.run(run())
