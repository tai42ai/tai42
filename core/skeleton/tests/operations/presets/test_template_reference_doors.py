"""Oracles for the stored-template ids the write doors check before they store anything.

A preset's schema ids are checked with its base tool, before any render; the ids a blob
names (through its base's declared ``template_refs`` extractor, else the base's input
annotations) are checked in the same walk that reads its tool names, at the preset, hook
and schedule doors. A miss is one 400 line per id, and a refused write stores nothing.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.backend import CrontabCadence
from tai42_contract.hooks import HookParams
from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_contract.tools import ReferenceHolder, TemplateRef

from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.app import instance
from tai42_skeleton.authz import execution
from tai42_skeleton.hooks import cache as hooks_cache
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import BadRequestError, OperationFailedError
from tai42_skeleton.operations import _authority as authority
from tai42_skeleton.operations import hooks as hooks_ops
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.operations import schedules as schedules_ops
from tai42_skeleton.operations.reference_graph import ExtractorFaultError, kwargs_references
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings
from tai42_skeleton.template import platform_referees
from tai42_skeleton.template.errors import TemplateNotFoundError

from ..._fakes.hook_runs import HookRunsHarness
from ..._fakes.recording_storage import RecordingStorage
from ..._fakes.tool_runs_redis import FakeRedis
from .conftest import _create

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures", "tests.app._fixtures.schedule_ext"],
    "tools": [
        {
            "title": "fx",
            "module": "tests.presets._fixtures",
            "include": ["weather", "echo", "plan_tool"],
            "extensions": {"echo": [["jqpass"]]},
        },
        {
            "title": "refs",
            "module": "tests.presets._ref_fixtures",
            "include": ["jq_refs_tool", "jq_shape_tool", "jq_keyerror_tool", "jq_str_tool", "keyerror_refs_tool"],
        },
        {
            "title": "sched",
            "module": "tests.app._fixtures.schedule_tools",
            "include": ["backend_list_schedules", "backend_delete_schedule", "backend_export_schedules"],
        },
    ],
    "agents": [{"title": "ag", "module": "tests.routers._authoring_fixtures", "include": ["tools_agent"]}],
}

_SCHEMA_MISS = "stored id 'schemas/missing' at output_schema is not a stored template"
_PROMPT_MISS = "stored id 'prompts/missing' at system_prompt is not a stored template"
_JQ_MISS = "stored id 'jq/missing' at jq_expression is not a stored template"


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authority, "access_control_settings", lambda: AccessControlSettings(enable=False))
    monkeypatch.setattr(execution, "access_control_settings", lambda: AccessControlSettings(enable=False))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RecordingStorage:
    provider = RecordingStorage({"schemas/out": (b'{"type": "object"}', "application/json")})
    monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
    return provider


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


def _within_app(body: Any) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await body()

    asyncio.run(run())


async def _stored(name: str) -> bool:
    try:
        await instance.app.presets.store.get_preset(name)
    except PresetNotFoundError:
        return False
    return True


async def _versions(name: str) -> list[int]:
    return [row["version"] for row in await preset_ops.list_versions(name=name)]


async def _save(name: str, **over: Any) -> dict[str, Any]:
    return await preset_ops.save_version(
        name=name,
        fixed_kwargs=over.get("fixed_kwargs"),
        extensions=None,
        output_schema=over.get("output_schema"),
        output_schema_provided="output_schema" in over,
        description=None,
    )


async def _refused(write: Any) -> str:
    with pytest.raises(BadRequestError) as caught:
        await write
    return caught.value.message


# -- a preset's schema ids, before any render ---------------------------------------------


def test_a_missing_schema_id_is_refused_before_the_render_at_create(pg, store) -> None:
    async def body() -> None:
        assert await _refused(_create("p", output_schema={"id": "schemas/missing"})) == _SCHEMA_MISS
        assert not await _stored("p")
        verdict = await preset_ops.validate_preset(
            name="p",
            base_tool="weather",
            description="d",
            output_schema_present=True,
            output_schema_value={"id": "schemas/missing"},
        )
        assert verdict == {"valid": False, "error": _SCHEMA_MISS}

    _within_app(body)


def test_a_missing_base_and_a_missing_schema_id_answer_both_lines(pg, store) -> None:
    async def body() -> None:
        message = await _refused(_create("p", base_tool="ghost", output_schema={"id": "schemas/missing"}))
        assert message == f"base tool 'ghost' is not a registered tool\n{_SCHEMA_MISS}"

    _within_app(body)


def test_a_missing_schema_id_is_refused_at_save_version_and_validate(pg, store) -> None:
    async def body() -> None:
        await _create("p", output_schema={"id": "schemas/out"})
        assert await _refused(_save("p", output_schema={"id": "schemas/missing"})) == _SCHEMA_MISS
        assert await _versions("p") == [1]
        verdict = await preset_ops.validate_preset(
            name="p", output_schema_present=True, output_schema_value={"id": "schemas/missing"}
        )
        assert verdict == {"valid": False, "error": _SCHEMA_MISS}

    _within_app(body)


def test_a_rollback_to_a_version_whose_schema_id_is_gone_is_refused(pg, store) -> None:
    async def body() -> None:
        store.put("schemas/missing", b'{"type": "object"}', "application/json")
        await _create("p", output_schema={"id": "schemas/missing"})
        await _save("p", output_schema=None)
        await store.delete("schemas/missing")
        assert await _refused(preset_ops.rollback_preset(name="p", version=1)) == _SCHEMA_MISS
        assert (await instance.app.presets.store.get_preset("p")).active_version == 2

    _within_app(body)


# -- a base tool's declared template extractor ---------------------------------------------


_EXTRACTED_MISS = "stored id 'jq/missing' at node 'fetch' expr is not a stored template"
_COMPOSED = {"plan": {"steps": [{"id": "a", "tool": "jq_refs_tool", "tool_kwargs": {}}]}}


def test_a_declared_extractor_names_its_ids_at_its_sites(pg, store) -> None:
    async def body() -> None:
        assert await _refused(_create("p", base_tool="jq_refs_tool")) == _EXTRACTED_MISS
        message = await _refused(_create("q", base_tool="plan_tool", fixed_kwargs=_COMPOSED))
        assert (
            message == "stored id 'jq/missing' at step 'a' > tool_kwargs > node 'fetch' expr is not a stored template"
        )
        assert not await _stored("p")
        assert not await _stored("q")
        store.put("jq/missing", b".")
        await _create("p", base_tool="jq_refs_tool")

    _within_app(body)


def test_the_hook_and_schedule_doors_read_a_declared_extractor(pg, store, hooks, scheduled) -> None:
    async def body() -> None:
        hook = hooks_ops.register_hook(
            name="h", topic="events", tool="plan_tool", execution_key="k", tool_kwargs=_COMPOSED
        )
        site = "tool_kwargs > step 'a' > tool_kwargs > node 'fetch' expr"
        chained = f"stored id 'jq/missing' at {site} is not a stored template"
        assert await _refused(hook) == chained
        assert await _refused(schedules_ops.create_schedule("jq_refs_tool", {})) == (
            "stored id 'jq/missing' at tool_kwargs > node 'fetch' expr is not a stored template"
        )
        assert await hooks.list_hooks() == {}
        assert scheduled == []

    _within_app(body)


def test_a_template_extractor_value_error_is_a_400(pg, store) -> None:
    async def body() -> None:
        assert await _refused(_create("p", base_tool="jq_shape_tool")) == "graph must be an object"
        verdict = await preset_ops.validate_preset(name="p", base_tool="jq_shape_tool", description="d")
        assert verdict == {"valid": False, "error": "graph must be an object"}

    _within_app(body)


@pytest.mark.parametrize(
    ("base", "error"),
    [
        ("jq_keyerror_tool", "template_refs extractor of base tool 'jq_keyerror_tool' raised KeyError"),
        (
            "jq_str_tool",
            "template_refs extractor of base tool 'jq_str_tool' returned a str entry, not a TemplateRef",
        ),
    ],
)
def test_a_broken_template_extractor_is_a_500_storing_nothing(pg, store, caplog, base: str, error: str) -> None:
    async def body() -> None:
        with caplog.at_level(logging.ERROR), pytest.raises(OperationFailedError) as caught:
            await _create("p", base_tool=base)
        assert caught.value.message == error
        assert not await _stored("p")
        assert [r for r in caplog.records if r.levelno == logging.ERROR and r.getMessage() == error]

    _within_app(body)


# -- an agent's input annotations ------------------------------------------------------------


def test_an_agent_preset_naming_a_missing_prompt_is_refused(pg, store) -> None:
    async def body() -> None:
        missing = {"system_prompt": {"id": "prompts/missing"}}
        assert await _refused(_create("p", base_tool="tools_agent", fixed_kwargs=missing)) == _PROMPT_MISS
        assert not await _stored("p")
        verdict = await preset_ops.validate_preset(
            name="p", base_tool="tools_agent", description="d", fixed_kwargs=missing
        )
        assert verdict == {"valid": False, "error": _PROMPT_MISS}
        await _create("p", base_tool="tools_agent")
        assert await _refused(_save("p", fixed_kwargs=missing)) == _PROMPT_MISS
        assert await _versions("p") == [1]

    _within_app(body)


def test_an_agent_preset_rollback_to_a_gone_prompt_is_refused(pg, store) -> None:
    async def body() -> None:
        store.put("prompts/missing", b"be brief")
        await _create("p", base_tool="tools_agent", fixed_kwargs={"system_prompt": {"id": "prompts/missing"}})
        await _save("p", fixed_kwargs={})
        await store.delete("prompts/missing")
        assert await _refused(preset_ops.rollback_preset(name="p", version=1)) == _PROMPT_MISS

    _within_app(body)


def test_a_subagent_prompt_is_read_at_its_place(pg, store) -> None:
    async def body() -> None:
        nested = {"subagents": [{"system_prompt": {"id": "prompts/missing"}}]}
        assert await _refused(_create("p", base_tool="tools_agent", fixed_kwargs=nested)) == (
            "stored id 'prompts/missing' at subagents[0].system_prompt is not a stored template"
        )

    _within_app(body)


def test_a_prompt_stored_only_as_a_locale_variant_is_refused(pg, store) -> None:
    async def body() -> None:
        missing = {"system_prompt": {"id": "prompts/missing"}}
        store.put("prompts/missing@he", b"shalom")
        assert await _refused(_create("p", base_tool="tools_agent", fixed_kwargs=missing)) == _PROMPT_MISS
        store.put("prompts/missing", b"hello")
        await _create("p", base_tool="tools_agent", fixed_kwargs=missing)
        await _create("q", base_tool="tools_agent", fixed_kwargs={"system_prompt": {"content": "inline"}})

    _within_app(body)


def test_the_hook_and_schedule_doors_read_an_agents_prompt(pg, store, hooks, scheduled) -> None:
    async def body() -> None:
        missing = {"system_prompt": {"id": "prompts/missing"}}
        hook = hooks_ops.register_hook(
            name="h", topic="events", tool="tools_agent", execution_key="k", tool_kwargs=missing
        )
        line = "stored id 'prompts/missing' at tool_kwargs > system_prompt is not a stored template"
        assert await _refused(hook) == line
        assert await _refused(schedules_ops.create_schedule("tools_agent", missing)) == line
        assert await hooks.list_hooks() == {}
        assert scheduled == []

    _within_app(body)


# -- an extension branch's own fields ------------------------------------------------------


def test_a_branch_tools_own_template_field_is_read_at_every_door(pg, store, hooks, scheduled) -> None:
    async def body() -> None:
        blob = {"jq_expression": {"id": "jq/missing"}}
        hook = hooks_ops.register_hook(
            name="h", topic="events", tool="echo_jqpass", execution_key="k", tool_kwargs=blob
        )
        line = "stored id 'jq/missing' at tool_kwargs > jq_expression is not a stored template"
        assert await _refused(hook) == line
        assert await _refused(schedules_ops.create_schedule("echo_jqpass", blob)) == line
        assert await hooks.list_hooks() == {}
        assert scheduled == []
        assert await _refused(_create("p", base_tool="echo_jqpass", fixed_kwargs=blob)) == _JQ_MISS
        verdict = await preset_ops.validate_preset(
            name="p", base_tool="echo_jqpass", description="d", fixed_kwargs=blob
        )
        assert verdict == {"valid": False, "error": _JQ_MISS}
        store.put("jq/missing", b".")
        await hooks_ops.register_hook(name="h", topic="events", tool="echo_jqpass", execution_key="k", tool_kwargs=blob)
        await schedules_ops.create_schedule("echo_jqpass", blob)
        await _create("p", base_tool="echo_jqpass", fixed_kwargs=blob)
        assert scheduled != []

    _within_app(body)


def test_a_branch_preset_save_version_and_rollback_read_its_own_field(pg, store) -> None:
    async def body() -> None:
        blob = {"jq_expression": {"id": "jq/missing"}}
        await _create("p", base_tool="echo_jqpass")
        assert await _refused(_save("p", fixed_kwargs=blob)) == _JQ_MISS
        store.put("jq/missing", b".")
        await _save("p", fixed_kwargs=blob)
        await _save("p", fixed_kwargs={})
        await store.delete("jq/missing")
        assert await _refused(preset_ops.rollback_preset(name="p", version=2)) == _JQ_MISS

    _within_app(body)


# -- the reader a plugin asks ----------------------------------------------------------------


def test_the_reader_answers_an_agents_ids_in_walk_order_stored_or_not(pg, store) -> None:
    async def body() -> None:
        tools = instance.app.tools
        blob = {"system_prompt": {"id": "prompts/a"}, "subagents": [{"system_prompt": {"id": "prompts/b"}}]}
        expected = [
            TemplateRef(id="prompts/a", site="system_prompt"),
            TemplateRef(id="prompts/b", site="subagents[0].system_prompt"),
        ]
        assert await tools.kwargs_template_refs("tools_agent", blob) == expected
        store.put("prompts/a", b"a")
        assert await tools.kwargs_template_refs("tools_agent", blob) == expected
        assert await tools.kwargs_template_refs("tools_agent", {"system_prompt": {"content": "x"}}) == []
        assert await tools.kwargs_template_refs("tools_agent", {}) == []
        await _create("plain", base_tool="tools_agent")
        assert await tools.kwargs_template_refs("plain", blob) == expected

    _within_app(body)


def test_the_reader_reads_a_branch_and_a_declared_extractor(pg, store) -> None:
    async def body() -> None:
        tools = instance.app.tools
        assert await tools.kwargs_template_refs("echo_jqpass", {"jq_expression": {"id": "jq/x"}}) == [
            TemplateRef(id="jq/x", site="jq_expression")
        ]
        assert await tools.kwargs_template_refs("jq_refs_tool", {"prompt": {"id": "t/one"}}) == [
            TemplateRef(id="jq/missing", site="node 'fetch' expr")
        ]

    _within_app(body)


def test_the_reader_chains_an_inline_presets_site_as_the_walk_does(pg, store, monkeypatch) -> None:
    async def body() -> None:
        inline = {"presets": [{"name": "fetcher", "description": "d", "base_tool": "jq_refs_tool"}]}
        refs = await instance.app.tools.kwargs_template_refs("tools_agent", inline)
        assert refs == [TemplateRef(id="jq/missing", site="kwargs.presets[0].fixed_kwargs > node 'fetch' expr")]
        walked = await kwargs_references("tools_agent", inline, "kwargs", body_root=True)
        assert [(ref.name, ref.site) for ref in walked if ref.registry == "template"] == [
            (ref.id, ref.site) for ref in refs
        ]
        holders = await _hook_holders(monkeypatch, "tools_agent", inline, frozenset({"jq/missing"}))
        assert [holder.locator["site"] for holder in holders] == [ref.site for ref in refs]

    _within_app(body)


async def _hook_holders(
    monkeypatch: pytest.MonkeyPatch, tool: str, tool_kwargs: dict[str, Any], ids: frozenset[str]
) -> list[ReferenceHolder]:
    """What the hook template-delete referee answers for one stored hook on ``tool``, seeded past the write door."""
    manager = InMemoryHooksManager(HooksSettings())
    params = HookParams(
        name="h",
        topic="events",
        tool=tool,
        execution_key="k",
        execution_key_fingerprint="fp",
        tool_kwargs=tool_kwargs,
    )
    await manager._store_hook(params)
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: manager)
    return await platform_referees._hook_referee(ids)


def test_the_reader_answers_nothing_for_an_unserved_name(pg, store, monkeypatch) -> None:
    async def body() -> None:
        blob = {"system_prompt": {"id": "prompts/a"}}
        assert await instance.app.tools.kwargs_template_refs("ghost", blob) == []
        assert await _hook_holders(monkeypatch, "ghost", blob, frozenset({"prompts/a"})) == []

    _within_app(body)


def test_the_reader_raises_a_value_free_error_and_extractor_faults(pg, store) -> None:
    async def body() -> None:
        tools = instance.app.tools
        prefix = "system_prompt is not a valid value for tool 'tools_agent': "
        with pytest.raises(ValueError, match=f"^{prefix}") as caught:
            await tools.kwargs_template_refs("tools_agent", {"system_prompt": 5})
        assert "5" not in str(caught.value).removeprefix(prefix)
        with pytest.raises(
            ExtractorFaultError, match=r"^template_refs extractor of base tool 'jq_keyerror_tool' raised KeyError$"
        ):
            await tools.kwargs_template_refs("jq_keyerror_tool", {})
        with pytest.raises(
            ExtractorFaultError, match=r"^tool_refs extractor of base tool 'keyerror_refs_tool' raised KeyError$"
        ):
            await tools.kwargs_template_refs("keyerror_refs_tool", {})

    _within_app(body)


# -- a preset with an authored input: its map is judged at run -------------------------------


_AGENT_IN_SCHEMA = {"type": "object", "properties": {"user_message": {"type": "string"}}}
_AGENT_IN_MAP = "{user_message: {content: .user_message}}"
_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {"user_message": {"type": "string"}, "prompt": {"type": "string"}},
    "additionalProperties": False,
}
_PROMPT_MAP = "{user_message: {content: .user_message}, system_prompt: {id: .prompt}}"
_RUN_MISS = "Template 'prompts/missing' not found."
_CRON = CrontabCadence(minute="0", hour="9")


async def _agent_in(input_schema: dict[str, Any], input_map: str, extensions: list[list[ExtensionElement]]) -> None:
    await preset_ops.create_preset(
        name="agent_in",
        base_tool="tools_agent",
        description="d",
        fixed_kwargs={},
        extensions=extensions,
        output_schema=None,
        input_schema=input_schema,
        input_map=input_map,
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The tool-run store, configured over the in-memory fake, so a hook fire writes its run record."""
    ops = importlib.import_module("tai42_skeleton.operations.tool_runs")
    fake = FakeRedis()
    settings = ToolRunsSettings()

    @asynccontextmanager
    async def ctx(client_cls: Any, s: Any = None, *, fresh: bool = False, **kwargs: Any) -> AsyncIterator[FakeRedis]:
        yield fake

    monkeypatch.setattr(ops, "client_ctx", ctx)
    monkeypatch.setattr(ops, "tool_runs_settings", lambda: settings)
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    return SimpleNamespace(ops=ops)


def test_a_preset_with_an_authored_input_has_its_blob_unread(pg, store, hooks) -> None:
    async def body() -> None:
        await _agent_in(_AGENT_IN_SCHEMA, _AGENT_IN_MAP, [])
        blob = {"user_message": "hi", "tool_names": "a,b"}
        await hooks_ops.register_hook(name="h", topic="events", tool="agent_in", execution_key="k", tool_kwargs=blob)
        assert list(await hooks.list_hooks()) == ["h"]
        assert await schedules_ops.create_schedule("agent_in", blob) == "system="
        assert await instance.app.tools.kwargs_template_refs("agent_in", blob) == []

    _within_app(body)


def test_a_template_id_the_map_places_is_judged_at_run(pg, store, hooks, recorded, caplog, monkeypatch) -> None:
    hook_runs = HookRunsHarness(monkeypatch)
    hook_runs.use(hooks)

    async def body() -> None:
        await _agent_in(_PROMPT_SCHEMA, _PROMPT_MAP, [["schedule_task"]])
        blob = {"user_message": "hi", "prompt": "prompts/missing"}
        await hooks_ops.register_hook(name="h", topic="events", tool="agent_in", execution_key="k", tool_kwargs=blob)
        await schedules_ops.create_schedule("agent_in", blob, cadence=_CRON, schedule_name="s")
        with caplog.at_level(logging.ERROR):
            [run_id] = (await hooks.on_event("events", {})).run_ids
            await hook_runs.close()
        runs = [await recorded.ops.get_run(run["run_id"]) for run in await recorded.ops.list_tool_runs("agent_in")]
        assert [(run["status"], run["error"]) for run in runs] == [("failed", _RUN_MISS)]
        [hook_run] = [run for run in (await hooks.list_hook_runs(None, 10, None)).items if run.id == run_id]
        hook_error = f"TemplateNotFoundError: {_RUN_MISS}"
        assert (hook_run.status, hook_run.attempts, hook_run.last_error) == ("failed", 1, hook_error)
        assert any(
            r.getMessage().startswith(f"hook run {run_id} (h on events) failed after 1 attempt(s)")
            and r.getMessage().endswith(f": {hook_error}")
            for r in caplog.records
        )
        job = _schedule_backend().store["s"]["kwargs"]
        fired = {key: value for key, value in job.items() if key in blob}
        with pytest.raises(TemplateNotFoundError, match=f"^{re.escape(_RUN_MISS)}$"):
            await instance.app.tools.run_tool("agent_in", fired)
        with pytest.raises(OperationFailedError) as caught:
            await schedules_ops.create_schedule("agent_in", blob)
        assert caught.value.message == "schedule creation failed (TemplateNotFoundError)"

    _within_app(body)


def _schedule_backend() -> Any:
    return importlib.import_module("tests.app._fixtures.schedule_tools")
