"""The synthesized agent ``run`` tool.

The run tool advertises the agent's ``ToolInput`` schema out-of-band (the
concrete per-field signature exists for extension composition, so ``parameters``
is set explicitly) and honors ``from_tool_input``'s set-fields-only contract:
only the caller-supplied fields reach ``run``, not every field materialized with
its default (an omitted optional arrives as the ``_UNSET`` sentinel and is
stripped). Agent run tools ARE extension-eligible — a manifest extension combo
targeting an agent name binds a branch composed over that concrete signature.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
import time
import warnings
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import Tool
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import TextContent
from pydantic.json_schema import PydanticJsonSchemaWarning
from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run

from tai42_skeleton.agent.session_thread import agent_session_thread
from tai42_skeleton.agent.thread_reservation import ReservedThreadNamespaceError
from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TurnTimeoutError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.plugins.quarantine import quarantined_plugins
from tai42_skeleton.tools.binding import _derive_input_schema
from tai42_skeleton.tools.turn_budget import TurnBudgetMiddleware, _turn_budget_armed, turn_budget


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


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
    from tai42_contract.interactions import SuspendedInteraction

    async def run() -> None:
        manifest = Manifest.model_validate(
            {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["parking"]}]}
        )
        async with app.app_context(manifest):
            result = await app.tools.run_tool("parking", {"text": "hi"})
            assert isinstance(result, SuspendedInteraction)
            assert result.interaction_id == "i-parked"

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
                    "jq_expression": "{text: .}",
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
                {"text": "hi", "jq_expression": "{text: .}", "next_tool_name": "shout"},
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


# -- the turn budget ----------------------------------------------------------


def _sleeper_manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["sleeper"]}]}
    )


def _plain_tools_manifest(*names: str) -> Manifest:
    return Manifest.model_validate(
        {"tools": [{"title": "tools", "module": "tests.agent._turn_budget_fixtures", "include": list(names)}]}
    )


@pytest.fixture
def set_turn_timeout(monkeypatch: pytest.MonkeyPatch):
    # Set the turn budget via env, drop the settings cache so the accessor rereads it,
    # and drop it again on teardown so the value cannot leak into later tests.
    def _set(value: str) -> None:
        monkeypatch.setenv("TAI_TURN_TIMEOUT_SECONDS", value)
        reset_all_settings()

    yield _set
    reset_all_settings()


def _fixture_flag(module: str, name: str) -> Any:
    # A fixtures module is popped from sys.modules and re-imported on each app_context
    # enter, so a tool/agent that ran mutates the LIVE module object; read the flag off
    # that one, not a stale import binding.
    return getattr(sys.modules[module], name)


def _budget_flag(name: str) -> Any:
    return _fixture_flag("tests.agent._turn_budget_fixtures", name)


def _sleeper_completed() -> bool:
    return bool(_fixture_flag("tests.agent._fixtures", "sleeper_completed"))


def test_turn_timeout_off_by_default_runs_unbounded():
    # No env set: the timeout is None, the run tool awaits the turn unbounded, and a
    # turn slower than any nonexistent limit still completes.
    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            assert await app.tools.run_tool("sleeper", {"seconds": 0.05}) == "done"

    asyncio.run(run())
    assert _sleeper_completed() is True


def test_turn_timeout_set_allows_a_run_under_the_limit(set_turn_timeout):
    set_turn_timeout("5")

    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            assert await app.tools.run_tool("sleeper", {"seconds": 0.0}) == "done"

    asyncio.run(run())
    assert _sleeper_completed() is True


def test_turn_timeout_exceeded_raises_typed_error_and_cancels_the_turn(set_turn_timeout):
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
                await app.tools.run_tool("sleeper", {"seconds": 5})

    asyncio.run(run())
    # The turn was cancelled on expiry, not left running past the deadline.
    assert _sleeper_completed() is False


def test_detached_run_ignores_the_timeout_and_runs_unbounded(set_turn_timeout):
    # A detached run (a background submit or a hook/trigger fire) has no live caller
    # holding a connection, so the turn budget does not apply: with the detached flag
    # set, a turn far slower than the limit still completes and no TurnTimeoutError is
    # raised — exactly as if the setting were unset.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_sleeper_manifest()):
            token = mark_detached_run()
            try:
                assert await app.tools.run_tool("sleeper", {"seconds": 0.2}) == "done"
            finally:
                reset_detached_run(token)

    asyncio.run(run())
    # The turn ran to its end past the deadline rather than being cancelled on expiry.
    assert _sleeper_completed() is True


def _inner_timeout_manifest() -> Manifest:
    return Manifest.model_validate(
        {"agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["inner_timeout"]}]}
    )


def test_inner_timeout_error_passes_through_not_reclassified(set_turn_timeout):
    # A builtin TimeoutError raised by the turn's OWN work (e.g. a jq budget abort)
    # under a generous turn budget reaches the caller unchanged — the wrapper must
    # not mistake it for the turn overrunning its limit.
    set_turn_timeout("300")

    async def run() -> None:
        async with app.app_context(_inner_timeout_manifest()):
            with pytest.raises(TimeoutError, match=r"inner budget exceeded") as excinfo:
                await app.tools.run_tool("inner_timeout", {"seconds": 0.0})
            assert not isinstance(excinfo.value, TurnTimeoutError)

    asyncio.run(run())


def test_plain_tool_over_budget_raises_turn_timeout_and_cancels(set_turn_timeout):
    # A NON-agent tool (a plain function tool) reached through the shared seam is
    # covered by the same budget: over-budget raises the typed error and the run is
    # cancelled on expiry (its completion flag stays False).
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("slow_tool")):
            with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
                await app.tools.run_tool("slow_tool", {"seconds": 5})

    asyncio.run(run())
    assert _budget_flag("slow_tool_completed") is False


def test_turn_timeout_names_the_parked_question_on_expiry(set_turn_timeout):
    # A turn killed while parked on an unanswered question names the question (and its
    # interaction id) in the expiry error rather than the bare generic timeout message:
    # the answer wait stamps the pending question on the cancellation that unwinds it,
    # and the wrapper reads it off the cancelled TimeoutError's cause.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("parked_question_tool")):
            with pytest.raises(TurnTimeoutError) as excinfo:
                await app.tools.run_tool("parked_question_tool", {"seconds": 5})
            message = str(excinfo.value)
            assert "turn exceeded the 0.05s turn timeout" in message
            assert "waiting on an unanswered question" in message
            assert "iid-1" in message
            assert "what is the status?" in message

    asyncio.run(run())
    assert _budget_flag("parked_question_completed") is False


def test_turn_timeout_redacts_a_sensitive_parked_question(set_turn_timeout):
    # A turn killed while parked on a SENSITIVE question names the interaction id but
    # NOT the question text — a credential prompt is redacted to ``[sensitive question]``.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("parked_sensitive_question_tool")):
            with pytest.raises(TurnTimeoutError) as excinfo:
                await app.tools.run_tool("parked_sensitive_question_tool", {"seconds": 5})
            message = str(excinfo.value)
            assert "turn exceeded the 0.05s turn timeout" in message
            assert "waiting on an unanswered question" in message
            assert "iid-9" in message
            assert "[sensitive question]" in message
            assert "what is your password?" not in message

    asyncio.run(run())
    assert _budget_flag("parked_sensitive_completed") is False


def test_turn_timeout_without_a_parked_question_keeps_the_generic_message(set_turn_timeout):
    # An over-budget turn that parked no question (nothing stamped the cancellation)
    # keeps the exact generic expiry message — naming the question is purely additive.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("slow_tool")):
            with pytest.raises(TurnTimeoutError) as excinfo:
                await app.tools.run_tool("slow_tool", {"seconds": 5})
            assert str(excinfo.value) == "turn exceeded the 0.05s turn timeout"

    asyncio.run(run())
    assert _budget_flag("slow_tool_completed") is False


def test_plain_tool_under_budget_completes(set_turn_timeout):
    set_turn_timeout("5")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("slow_tool")):
            assert await app.tools.run_tool("slow_tool", {"seconds": 0.0}) == "slow-done"

    asyncio.run(run())
    assert _budget_flag("slow_tool_completed") is True


def test_nested_dispatch_bounded_by_outer_window_no_rearm(set_turn_timeout):
    # A nested dispatch through the seam (``nested_outer`` -> ``run_tool`` ->
    # ``nested_inner``) does NOT re-arm a fresh window: the inner call sees the budget
    # already armed by the outer call and opens none of its own, so the single OUTER
    # window bounds the whole turn and cancels the slow inner mid-run.
    set_turn_timeout("0.1")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("nested_outer", "nested_inner")):
            with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.1s turn timeout"):
                await app.tools.run_tool("nested_outer", {"seconds": 5})

    asyncio.run(run())
    # The inner tool observed the budget already armed (the guard), so it opened no
    # window of its own; the outer window cancelled it before it could complete.
    assert _budget_flag("nested_inner_saw_armed") is True
    assert _budget_flag("nested_inner_completed") is False


def test_nested_turn_budget_reuses_outer_window_and_disarms_cleanly(set_turn_timeout):
    # Directly on the arming primitive: the outer call arms the guard, a nested call
    # sees it armed and adds no arming of its own, exiting the nested block leaves the
    # outer arming intact, and only the outer exit disarms.
    set_turn_timeout("5")

    async def run() -> None:
        assert _turn_budget_armed.get() is False
        async with turn_budget():
            assert _turn_budget_armed.get() is True
            async with turn_budget():
                assert _turn_budget_armed.get() is True
            assert _turn_budget_armed.get() is True
        assert _turn_budget_armed.get() is False

    asyncio.run(run())


def test_turn_budget_unset_runs_unbounded():
    # No env set: the budget is None, so the arming primitive is a pure pass-through —
    # it never arms and a block far slower than any nonexistent limit still completes.
    async def run() -> None:
        async with turn_budget():
            assert _turn_budget_armed.get() is False
            await asyncio.sleep(0.05)

    asyncio.run(run())


# -- the MCP tool-call edge middleware ----------------------------------------


def test_edge_middleware_over_budget_raises_turn_timeout(set_turn_timeout):
    # The MCP session ``tools/call`` edge arms the budget itself (it never reaches the
    # ``run_tool`` seam): ``TurnBudgetMiddleware.on_call_tool`` wraps ``call_next`` in
    # the budget, so a call_next slower than the limit is cancelled on expiry and raises
    # the typed error.
    set_turn_timeout("0.05")
    mw = TurnBudgetMiddleware()

    async def call_next(_ctx):
        await asyncio.sleep(5)
        return "reached"

    async def run() -> None:
        with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
            await mw.on_call_tool(cast("MiddlewareContext[Any]", object()), call_next)

    asyncio.run(run())


def test_edge_middleware_arms_once_nested_run_tool_sees_armed_no_rearm(set_turn_timeout):
    # The edge middleware arms the budget BEFORE call_next runs; when call_next reaches
    # the shared ``run_tool`` seam, the seam sees the guard already armed and opens no
    # second window — the single edge window bounds the whole turn.
    set_turn_timeout("5")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("nested_inner")):
            mw = TurnBudgetMiddleware()

            async def call_next(_ctx):
                # The edge already armed the budget: the guard is set before the seam.
                assert _turn_budget_armed.get() is True
                return await app.tools.run_tool("nested_inner", {"seconds": 0.0})

            assert await mw.on_call_tool(cast("MiddlewareContext[Any]", object()), call_next) == "inner-done"

    asyncio.run(run())
    # The seam ran under the guard the edge armed, so it opened no window of its own.
    assert _budget_flag("nested_inner_saw_armed") is True
    assert _budget_flag("nested_inner_completed") is True


# -- the MCP tool-call edge secret reveal -------------------------------------


def test_mcp_edge_reveals_wrapped_secret_to_the_client():
    # The MCP ``tools/call`` edge is a live-caller door: a wrapped secret in the tool's
    # return is revealed before FastMCP serializes the result, so an in-memory MCP client
    # receives the real value in both structured and text content — never the placeholder.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def vault() -> dict:
                """Return a dict carrying a wrapped secret."""
                return {"token": SecretValue("tok-4242-xyzzy")}

            async with Client(app._fast_mcp) as client:
                result = await client.call_tool("vault", {})

            assert result.structured_content == {"token": "tok-4242-xyzzy"}
            texts = [block.text for block in result.content if isinstance(block, TextContent)]
            assert any("tok-4242-xyzzy" in text for text in texts)

    asyncio.run(run())


def test_mcp_edge_reveals_wrapped_secret_of_a_preset_to_the_client():
    # A PRESET reached over the MCP edge goes FastMCP -> TransformedTool.run directly
    # (the in-process reveal gate is unarmed), so the parent tool's convert_result
    # reveals: the live MCP caller of a preset receives the real value, exactly as a
    # direct tool call does.
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def vault(account: str) -> dict:
                """Return an account's stored token wrapped as a secret."""
                return {"account": account, "token": SecretValue(f"tok-{account}")}

            await app.preset_manager.register("acme_vault", "vault", {"account": "acme"}, [], "Acme vault")
            try:
                async with Client(app._fast_mcp) as client:
                    result = await client.call_tool("acme_vault", {})
            finally:
                await app.preset_manager.remove("acme_vault")

            assert result.structured_content == {"account": "acme", "token": "tok-acme"}

    asyncio.run(run())


class _LogCapture(logging.Handler):
    """Captures each emitted record's FULLY formatted text — message plus the
    exception traceback ``logger.exception`` attaches — so a leaked secret in either
    is detectable."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.texts.append(self.format(record))


def test_mcp_edge_secret_preset_schema_violation_redacts_the_secret_from_logs_and_client():
    # A PRESET over a secret-returning tool, reached over the live MCP edge with an
    # output_schema the REAL token VIOLATES (``minLength: 6`` vs the 5-char ``tok-x``,
    # which the 8-char ``[secret]`` placeholder would falsely satisfy): the reveal gate
    # is UNARMED, so convert_result reveals the secret into structured_content and the
    # output-schema guard validates the revealed value. The violation raises loudly, but
    # the plaintext token must ride NEITHER fastmcp's ``logger.exception`` NOR the error
    # returned to the client — the redacted failure keeps only the json_path and the
    # placeholder.
    schema = {
        "type": "object",
        "properties": {"account": {"type": "string"}, "token": {"type": "string", "minLength": 6}},
        "required": ["account", "token"],
    }
    capture = _LogCapture()
    server_logger = logging.getLogger("fastmcp.server.server")

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def vault(account: str) -> dict:
                """Return an account's stored token wrapped as a secret."""
                return {"account": account, "token": SecretValue(f"tok-{account}")}

            await app.preset_manager.register(
                "x_vault", "vault", {"account": "x"}, [], "Short vault", output_schema=schema
            )
            server_logger.addHandler(capture)
            try:
                async with Client(app._fast_mcp) as client:
                    with pytest.raises(ToolError) as caught:
                        await client.call_tool("x_vault", {})
            finally:
                server_logger.removeHandler(capture)
                await app.preset_manager.remove("x_vault")

            client_error = str(caught.value)
            # The client-visible error carries the redacted failure, never the real token.
            assert "tok-x" not in client_error
            assert "$.token" in client_error
            assert SECRET_PLACEHOLDER in client_error

            # The failure WAS logged (not swallowed), and the real token appears in NO
            # log record — neither its message nor the attached traceback.
            assert capture.texts, "expected fastmcp to log the failed tool call"
            assert all("tok-x" not in text for text in capture.texts)

    asyncio.run(run())


def test_mcp_edge_non_secret_preset_schema_violation_quotes_the_offending_instance():
    # A NON-secret preset over the same live MCP edge whose plain result violates its
    # output_schema: no secret was revealed on this door, so the secret redaction does
    # NOT widen here — the raised error still quotes the offending instance verbatim.
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "units": {"type": "string", "maxLength": 2}},
        "required": ["city", "units"],
    }

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def weather(city: str, units: str = "metric") -> dict:
                """Report the weather for a city."""
                return {"city": city, "units": units}

            await app.preset_manager.register(
                "p_bad", "weather", {"units": "imperial"}, [], "bad", output_schema=schema
            )
            try:
                async with Client(app._fast_mcp) as client:
                    with pytest.raises(ToolError) as caught:
                        await client.call_tool("p_bad", {"city": "x"})
            finally:
                await app.preset_manager.remove("p_bad")

            # The baked ``imperial`` (7 chars) is the offending value; the non-secret
            # door keeps instance-quoting, so it rides the error unchanged.
            assert "imperial" in str(caught.value)

    asyncio.run(run())


def test_offloaded_sync_tool_outruns_cancellation_and_completes_in_background(set_turn_timeout):
    # Python cannot cancel a running thread: when a plain (sync) tool is offloaded via
    # ``asyncio.to_thread`` and the budget expires, the awaiting caller is answered with
    # ``TurnTimeoutError``, but the thread runs to completion in the background — the real
    # boundary of the turn budget.
    set_turn_timeout("0.05")

    async def run() -> None:
        async with app.app_context(_plain_tools_manifest("blocking_sync_tool")):
            with pytest.raises(TurnTimeoutError, match=r"turn exceeded the 0\.05s turn timeout"):
                await app.tools.run_tool("blocking_sync_tool", {"seconds": 0.4}, offload_sync=True)

    asyncio.run(run())
    # The caller was answered on expiry, yet the offloaded thread could not be interrupted
    # and recorded its completion in the background.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _budget_flag("blocking_sync_completions"):
        time.sleep(0.02)
    assert list(_budget_flag("blocking_sync_completions")) == ["blocking_sync_tool"]
