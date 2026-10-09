"""The run-entry drain at the doors of a booted app: each door waits for its subject's pending saves once.

The synchronous run-tool door drains at its ``visit`` and its nested ``run_tool`` dispatch on the same
task adds no query; the MCP edge drains at ``dispatch_scope``; a call naming no subject drains
nothing; a deferred call's own dispatch skips its save's subjects. A held subject answers 409 (or
503 for a save that did not finish) at the run-tool door and a named refusal at the MCP edge. The
drain itself is spied (its behaviour on real rows is proven in ``tests/states/outbox``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from tai42_contract.app import tai42_app
from tai42_contract.states import StateSubject
from tai42_contract.states.errors import StatePendingSaveFailedError, StatePendingSaveTimeoutError
from tai42_contract.states.models import StateContext, SubjectCandidates

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError, UnavailableError
from tai42_skeleton.operations import tools as tools_ops
from tai42_skeleton.states.outbox import drain as drain_mod
from tai42_skeleton.states.outbox.drain import outbox_apply_scope

from .._drain_spy import spy_on_drains, subject_key

_SUBJECT = StateSubject(target_kind="tool", target_name="probe_door", kind="thread", key="t-1")
_KEY = subject_key("tool", "probe_door", "thread", "t-1")


def _register_probe_tools(inner_calls: list[str]) -> None:
    @app.tools.tool(force=True)
    async def probe_inner() -> str:
        """A tool another tool calls."""
        inner_calls.append("inner")
        return "inner"

    @app.tools.tool(force=True)
    async def probe_door() -> str:
        """A tool that calls another tool on its own task."""
        return await tai42_app.tools.run_tool("probe_inner", {})


def test_the_run_tool_door_drains_once_and_its_nested_dispatch_adds_none(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = spy_on_drains(monkeypatch)
    inner: list[str] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools(inner)
            result = await tools_ops.run_tool("probe_door", {}, subject=_SUBJECT)
            assert result == "inner"

    asyncio.run(run())
    assert inner == ["inner"]
    assert spy.keys == [[_KEY]]  # the visit's drain; the two nested dispatches on its task query nothing


@pytest.mark.parametrize(
    ("error", "door_error", "status"),
    [
        (StatePendingSaveFailedError("subject x has a failed pending save 7", save_id="7"), ConflictError, 409),
        (
            StatePendingSaveTimeoutError("subject x still has pending save 7 after 30.0s", save_id="7"),
            UnavailableError,
            503,
        ),
    ],
)
def test_the_run_tool_door_answers_a_held_or_slow_subject(
    monkeypatch: pytest.MonkeyPatch, error: Exception, door_error: type[Exception], status: int
) -> None:
    async def _refuse(service: Any, keys: Any, deadline: float, *, applying: Any = None) -> None:
        raise error

    monkeypatch.setattr(drain_mod, "_states_on", lambda: True)
    monkeypatch.setattr(drain_mod, "drain_subjects", _refuse)
    monkeypatch.setattr(drain_mod, "live_states_service", lambda: None)
    ran: list[str] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools(ran)
            with pytest.raises(door_error) as raised:
                await tools_ops.run_tool("probe_door", {}, subject=_SUBJECT)
            assert raised.value.status == status  # type: ignore[attr-defined]
            assert raised.value.extra == {"save_id": "7"}  # type: ignore[attr-defined]
            assert str(raised.value) == str(error)

    asyncio.run(run())
    assert ran == []  # the refusal came before anything ran


def test_the_mcp_edge_drains_once_at_dispatch_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = spy_on_drains(monkeypatch)
    inner: list[str] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools(inner)
            async with Client(app._fast_mcp) as client:
                result = await client.call_tool("probe_door", {}, meta={"tai42/subject": _SUBJECT.model_dump()})
            assert result.data == "inner"

    asyncio.run(run())
    assert spy.keys == [[_KEY]]


def test_the_mcp_edge_answers_a_held_subject_with_a_named_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "subject tool/probe_door/thread/t-1 has a failed pending save 7; an operator must retry or discard it"

    async def _refuse(service: Any, keys: Any, deadline: float, *, applying: Any = None) -> None:
        raise StatePendingSaveFailedError(message, save_id="7")

    monkeypatch.setattr(drain_mod, "_states_on", lambda: True)
    monkeypatch.setattr(drain_mod, "drain_subjects", _refuse)
    monkeypatch.setattr(drain_mod, "live_states_service", lambda: None)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools([])
            async with Client(app._fast_mcp) as client:
                result = await client.call_tool(
                    "probe_door", {}, meta={"tai42/subject": _SUBJECT.model_dump()}, raise_on_error=False
                )
            assert result.is_error is True
            assert [block.text for block in result.content] == [message]  # type: ignore[union-attr]

    asyncio.run(run())


def test_a_call_naming_no_subject_drains_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = spy_on_drains(monkeypatch)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools([])
            assert await tai42_app.tools.run_tool("probe_door", {}) == "inner"

    asyncio.run(run())
    assert spy.queries == []


def test_a_deferred_calls_own_dispatch_skips_its_saves_subjects(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.tools.deferred import ToolCallKind

    spy = spy_on_drains(monkeypatch)
    ctx = StateContext(
        door="api",
        candidates=SubjectCandidates(target_kind="tool", target_name="probe_door", by_kind={"thread": "t-1"}),
    )

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools([])
            payload = {"tool": "probe_door", "arguments": {}, "state_context": ctx.model_dump(mode="json")}
            with outbox_apply_scope(9, frozenset({_KEY}), "me"):
                await ToolCallKind().apply(payload, idempotency_key="9:0")

    asyncio.run(run())
    assert spy.queries == []


def _stub_identity_binds(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> None:
    """The doors bind an execution identity first; that bind is not what these tests prove."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _bind(execution_key: str, *, bound_fingerprint: str):
        yield None

    for module in modules:
        monkeypatch.setattr(module, "bind_execution_identity", _bind)


@pytest.mark.parametrize("run_store", ["off", "on"])
def test_a_hook_fire_drains_at_its_visit_and_a_recorded_child_run_re_checks(
    monkeypatch: pytest.MonkeyPatch, run_store: str
) -> None:
    from contextlib import asynccontextmanager

    from tai42_contract.hooks import HookParams, HookSubject
    from tai42_contract.template import TemplatedText

    from tai42_skeleton.hooks.managers import base_hooks_manager
    from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
    from tai42_skeleton.operations import tool_runs as tool_runs_pkg

    from .._fakes.tool_runs_redis import FakeRedis

    spy = spy_on_drains(monkeypatch)
    _stub_identity_binds(monkeypatch, base_hooks_manager)
    if run_store == "on":
        fake = FakeRedis()

        @asynccontextmanager
        async def _ctx(client_cls: Any, settings: Any = None, *, fresh: bool = False, **kwargs: Any):
            yield fake

        monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(tool_runs_pkg, "client_ctx", _ctx)
    hook = HookParams(
        name="h",
        topic="t",
        tool="probe_door",
        execution_key="svc-key",
        execution_key_fingerprint="fp",
        subject=HookSubject(
            target_kind="tool", target_name="probe_door", kind="thread", key_expr=TemplatedText(content=".thread")
        ),
    )
    inner: list[str] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools(inner)
            await InMemoryHooksManager._run_hook(hook, {"thread": "t-1"})

    asyncio.run(run())
    assert inner == ["inner"]
    if run_store == "off":
        # The fire runs the tool inline on the hook's own task: its visit's drain covers the dispatch.
        assert spy.keys == [[_KEY]]
    else:
        # A recorded fire runs the tool in a supervised child task, whose dispatch re-checks once.
        assert spy.keys == [[_KEY], [_KEY]]
        assert spy.tasks[0] is not spy.tasks[1]


def test_a_detached_continuation_and_a_form_reaction_drain_at_dispatch_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.interactions import continuation, reaction

    spy = spy_on_drains(monkeypatch)
    _stub_identity_binds(monkeypatch, continuation)
    monkeypatch.setattr("tai42_skeleton.authz.execution.bind_execution_identity", _stub_bind())
    ctx = StateContext(
        door="api",
        candidates=SubjectCandidates(target_kind="tool", target_name="probe_door", by_kind={"thread": "t-1"}),
    )
    seen: list[str] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):

            @app.tools.tool(force=True)
            async def probe_resume(interaction_id: str, answer: Any) -> str:
                """The continuation a park resumes."""
                seen.append(f"resume:{answer}")
                return "resumed"

            @app.tools.tool(force=True)
            async def probe_react(interaction_id: str, event: dict, values: dict) -> dict:
                """The reaction handler a form calls."""
                seen.append("react")
                return {}

            await continuation._run_continuation("svc-key", "fp", "probe_resume", "i-1", "yes", park_context=ctx)
            await reaction._run_reaction(
                reaction_tool="probe_react",
                identity="svc-key",
                fingerprint="fp",
                state_ctx=ctx,
                asked_by=[],
                interaction_id="i-1",
                event={},
                values={},
                deadline=5.0,
            )

    asyncio.run(run())
    assert seen == ["resume:yes", "react"]
    assert spy.keys == [[_KEY], [_KEY]]  # each at its own outermost dispatch


def _stub_bind() -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _bind(execution_key: str, *, bound_fingerprint: str):
        yield None

    return _bind


def test_a_crash_resume_re_drive_drains_again_in_its_new_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.operations.tool_runs import supervisor
    from tai42_skeleton.states.context import state_context
    from tai42_skeleton.states.outbox.drain import run_entry_drain

    spy = spy_on_drains(monkeypatch)
    ctx = StateContext(
        door="api",
        candidates=SubjectCandidates(target_kind="tool", target_name="probe_door", by_kind={"thread": "t-1"}),
    )

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools([])

            async def _re_drive() -> None:
                with state_context(ctx):
                    await supervisor.run_recorded("probe_door", {})

            # A read path that itself runs inside an ordered entry on the same subject spawns the
            # re-drive as a plain task: the inherited mark never skips the re-drive's own drain.
            with state_context(ctx):
                async with run_entry_drain():
                    await asyncio.create_task(_re_drive())

    asyncio.run(run())
    assert spy.keys == [[_KEY], [_KEY]]
    assert spy.tasks[0] is not spy.tasks[1]


def test_a_schedule_fire_drains_its_subject_once_at_its_visit(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.backend.schedule_fire import fire_schedule_door

    spy = spy_on_drains(monkeypatch)
    inner: list[str] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools(inner)
            await fire_schedule_door(
                "probe_door", {}, subject=_SUBJECT, state_binding=None, contract=None, receives_outcome=True
            )

    asyncio.run(run())
    assert inner == ["inner"]
    assert spy.keys == [[_KEY]]


@pytest.mark.parametrize("forwarded", [True, False])
def test_a_schedule_callback_drains_only_a_forwarded_subject(monkeypatch: pytest.MonkeyPatch, forwarded: bool) -> None:
    from tai42_kit.backend.callback import CallbackSchema, _run_callback_tool
    from tai42_kit.utils.schedule_subject import SCHEDULE_SUBJECT_ARG

    spy = spy_on_drains(monkeypatch)
    carried = {SCHEDULE_SUBJECT_ARG: _SUBJECT.model_dump()} if forwarded else {}
    inner: list[str] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            _register_probe_tools(inner)
            await _run_callback_tool(CallbackSchema(tool="probe_door", carried_kwargs=carried), {})

    asyncio.run(run())
    assert inner == ["inner"]
    # A forwarded subject drives the follow-up through a visit, which drains; with nothing
    # forwarded the follow-up runs under no state context — there is no subject to wait for.
    assert spy.keys == ([[_KEY]] if forwarded else [])
