"""A parked run applies its door's DEFERRED binding updates at its real terminal.

The door that STARTS a run deposits a state binding; the binding's UPDATES apply once, on a clean
output. A run that PARKS to ask someone is not a clean output, so the live dispatch drops the
updates — and this suite proves the platform instead DEFERS them and applies them once at the run's
real terminal, over the terminal output, whichever door started the run (the in-process run_tool
seam and the MCP ``tools/call`` edge) and whichever path drives the resume (an answer and the
expiry reaper's redelivery). It also proves a person erase scrubs the stored run input the park
carries, and that a failed terminal applies nothing (matching the live apply).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tai42_contract.access_control import reset_request_user_id, set_request_user_id
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    RunTerminalFailed,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.states import (
    ApplyResult,
    StateAttach,
    StateBinding,
    StateInjection,
    StateRecord,
    StateUpdate,
    TemplateJqApplyResult,
)
from tai42_contract.template import TemplatedText
from tai42_contract.tools import ToolInvocation, reset_current_tool_invocation, set_current_tool_invocation

from tai42_skeleton.app.instance import app
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions import kill as kill_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store.records import ContinuationDue
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import interactions as ops

from .._fakes.interactions_redis import FakeRedis


class _FakeStates:
    """The states facet patched onto the real instance: records the binding runtime's writes."""

    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self._record = record or {}
        self.applies: list[tuple[str, Any]] = []

    def patch_onto(self, facet: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # The facet is the process app's single, long-lived ``_states_facet`` instance, so the
        # fakes go on through monkeypatch and are lifted at teardown — a raw assignment would
        # leave them on the shared facet and answer an unrelated later test's states door.
        monkeypatch.setattr(facet, "context", self.context)
        monkeypatch.setattr(facet, "read", self.read)
        monkeypatch.setattr(facet, "apply_template_jq", self.apply_template_jq)
        monkeypatch.setattr(facet, "apply", self.apply)
        monkeypatch.setattr(facet, "apply_batch", self.apply_batch)

    def context(self):
        return None

    async def read(self, state, subject):
        return StateRecord(state=state, subject=subject, data=self._record, seq=1.0, canonical_subject=subject)

    async def apply_template_jq(self, state, subject, name, input_, *, op_id, origin) -> TemplateJqApplyResult:
        self.applies.append((name, input_))
        return TemplateJqApplyResult(name=name, applied=True, data={}, seq=1.0, skipped=[])

    async def apply(self, state, subject, ops, *, op_id, origin) -> ApplyResult:
        self.applies.append(("__custom__", ops))
        return ApplyResult(applied=True, data={}, seq=1.0, skipped=[])

    async def apply_batch(self, writes) -> list[ApplyResult]:
        results: list[ApplyResult] = []
        for item in writes:
            if item.ops is not None:
                results.append(
                    await self.apply(item.state, item.subject, item.ops, op_id=item.op_id, origin=item.origin)
                )
                continue
            outcome = await self.apply_template_jq(
                item.state, item.subject, item.template_jq, item.input, op_id=item.op_id, origin=item.origin
            )
            results.append(
                ApplyResult(applied=outcome.applied, data=outcome.data, seq=outcome.seq, skipped=outcome.skipped)
            )
        return results


_SUBJECT_EXPR = TemplatedText(content='{target_kind: "agent", target_name: "a", kind: "thread", key: (.x | tostring)}')


def _door_binding() -> StateBinding:
    """A door binding whose custom update sets ``last`` from the terminal output's ``.ok``."""
    return StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=_SUBJECT_EXPR,
                input_injections=[StateInjection(jq=TemplatedText(content="{n: .n}"), into="injected")],
                updates=[StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["last"], value: .ok}]'))],
            )
        ]
    )


@pytest.fixture(autouse=True)
def _store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def _wire(monkeypatch, fake: FakeRedis) -> SimpleNamespace:
    settings = InteractionsSettings()

    from contextlib import asynccontextmanager

    from tai42_skeleton.access_control.settings import AccessControlSettings
    from tai42_skeleton.authz import execution as execution_module

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    for module in (ops, helper_module, continuation_module, kill_module):
        monkeypatch.setattr(module, "client_ctx", _ctx)
        monkeypatch.setattr(module, "interactions_settings", lambda: settings)
    # The bound execution identity would otherwise drive run_tool through the real access-control
    # policy store; disable it so the dispatch reaches the tool without a live Redis.
    monkeypatch.setattr(execution_module, "access_control_settings", lambda: AccessControlSettings(enable=False))
    return SimpleNamespace(settings=settings, store=InteractionStore(settings.key_prefix), fake=fake)


def _bind_driver():
    """Bind the resume continuation tool + the execution identity a parking run needs."""
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    return tool_token, id_token


def _unbind_driver(tokens) -> None:
    tool_token, id_token = tokens
    reset_execution_identity(id_token)
    reset_resume_continuation_tool(tool_token)


def _terminal_stub(output: Any):
    """A ``_run_continuation`` stub that returns a real terminal ``output`` (never a re-park)."""

    async def _stub(
        identity,
        fingerprint,
        tool,
        interaction_id,
        answer,
        park_context=None,
        park_asked_by=(),
        caller_ask_landing=None,
        chain_keys=(),
        *,
        mark_detached=True,
    ):
        return output

    return _stub


async def _drain() -> None:
    # Let the detached continuation drive(s) run to completion: wait for the module's in-flight
    # task set to empty (bounded), then settle any trailing callbacks.
    for _ in range(50):
        tasks = {t for t in continuation_module._CONTINUATION_TASKS if not t.done()}
        if not tasks:
            break
        await asyncio.wait(tasks, timeout=0.1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_in_process_park_defers_door_updates_to_the_real_terminal(monkeypatch, fake_redis):
    # A bound target whose run PARKS to ask writes NOTHING at the pause, then applies its door's
    # update ONCE at its real terminal output — the headline defect this plan closes.
    wired = _wire(monkeypatch, fake_redis)
    fake_states = _FakeStates(record={"n": 4})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake_states.patch_onto(app._states_facet, monkeypatch)

            deadline = datetime.now(UTC) + timedelta(hours=1)

            @app.tools.tool(force=True)
            async def parker(x: int, injected: dict | None = None) -> SuspendedInteraction:
                """A bound tool that async-parks instead of returning a clean output."""
                return await helper_module.ask("proceed?", mode="async", expiry_at=deadline)

            await app.preset_manager.register(
                "pp", "parker", {}, [], "a parking preset", state_binding=_door_binding(), version=1
            )

            tokens = _bind_driver()
            inv = set_current_tool_invocation(ToolInvocation(tool_name="pp", state_binding=_door_binding()))
            try:
                parked = await app.tools.run_tool("pp", {"x": 5})
            finally:
                reset_current_tool_invocation(inv)
                _unbind_driver(tokens)

            assert isinstance(parked, SuspendedInteraction)
            iid = parked.interaction_id
            # The pause applied NO update (a park is not a clean output).
            assert fake_states.applies == []
            # The park captured the door's merged binding + run input + door id.
            state = await wired.store.get_state(wired.fake, iid)
            assert state is not None
            assert state.request.deferred_binding is not None
            assert state.request.run_input is not None
            assert state.request.run_input["x"] == 5
            assert state.request.door_id == "pp"

            # The run reaches its real terminal with a clean output; the deferred update now applies.
            monkeypatch.setattr(continuation_module, "_run_continuation", _terminal_stub({"ok": 7}))
            token = set_request_user_id("answerer-x")
            try:
                assert await ops.answer_interaction(iid, "go") == {"interaction_id": iid, "status": "answered"}
            finally:
                reset_request_user_id(token)
            await _drain()

            assert ("__custom__", [{"op": "set", "path": ["last"], "value": 7}]) in fake_states.applies

    await run()


async def test_mcp_edge_park_defers_door_updates_to_the_real_terminal(monkeypatch, fake_redis):
    # The SAME deferral holds for a run started at the MCP ``tools/call`` edge: the edge shares the
    # dispatch chokepoint, so a park there captures the binding and applies it at the terminal too.
    from fastmcp.tools.base import ToolResult
    from tai42_contract.interactions import read_suspended_interaction_marker, suspended_interaction_marker

    from tai42_skeleton.tools.dispatch_scope import DispatchScopeMiddleware

    wired = _wire(monkeypatch, fake_redis)
    fake_states = _FakeStates(record={"n": 4})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake_states.patch_onto(app._states_facet, monkeypatch)

            deadline = datetime.now(UTC) + timedelta(hours=1)

            @app.tools.tool(force=True)
            async def parker(x: int, injected: dict | None = None) -> SuspendedInteraction:
                """A bound tool that async-parks at the MCP edge."""
                return await helper_module.ask("proceed?", mode="async", expiry_at=deadline)

            await app.preset_manager.register(
                "pm", "parker", {}, [], "a parking preset", state_binding=_door_binding(), version=1
            )

            mw = DispatchScopeMiddleware(app)
            message = SimpleNamespace(name="pm", arguments={"x": 5})
            context = SimpleNamespace(message=message, fastmcp_context=None)

            async def call_next(_ctx: object) -> ToolResult:
                # The edge has already armed the dispatch scope (and injected into ``message.arguments``);
                # run the bound tool here as the edge would, inside that armed scope, so its park
                # captures the deferred binding off the edge's own dispatch. Surface the park as the
                # wire marker the edge recognises.
                suspended = await parker(**message.arguments)
                return ToolResult(
                    structured_content=suspended_interaction_marker(suspended.interaction_id, suspended.expiry_at)
                )

            tokens = _bind_driver()
            try:
                result = await mw.on_call_tool(cast(Any, context), call_next)
            finally:
                _unbind_driver(tokens)

            marker = read_suspended_interaction_marker(result.structured_content)
            assert marker is not None
            iid = marker["interaction_id"]
            assert fake_states.applies == []  # the park applied nothing
            state = await wired.store.get_state(wired.fake, iid)
            assert state is not None
            assert state.request.deferred_binding is not None  # captured at the edge

            monkeypatch.setattr(continuation_module, "_run_continuation", _terminal_stub({"ok": 11}))
            token = set_request_user_id("answerer-x")
            try:
                assert await ops.answer_interaction(iid, "go") == {"interaction_id": iid, "status": "answered"}
            finally:
                reset_request_user_id(token)
            await _drain()

            assert ("__custom__", [{"op": "set", "path": ["last"], "value": 11}]) in fake_states.applies

    await run()


async def test_answer_copies_deferred_binding_into_the_continuation_due(monkeypatch, fake_redis):
    # The answer claim denormalizes the park's deferred binding + run input + door id onto the
    # continuation-due record, so the reaper's detached redelivery carries them without re-reading
    # the request.
    wired = _wire(monkeypatch, fake_redis)
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=1)
    binding = _door_binding()
    req = InteractionRequest(
        interaction_id="d1",
        group_id="dg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("d1"),
        created_at=now,
        timeout_at=deadline,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=deadline,
        on_expiry="resume",
        run_delivery_id="rd-1",
        deferred_binding=binding,
        run_input={"x": 5, "n": 4},
        door_id="pp",
    )
    await wired.store.add(wired.fake, req, idle_ttl=86400, continuation_fingerprint="fp-1", run_delivery_id="rd-1")
    # Block the fire so the continuation-due record stands to be inspected before it is cleared.
    release = asyncio.Event()

    async def _block(*args, **kwargs):
        await release.wait()
        return SuspendedInteraction(interaction_id="d1")

    monkeypatch.setattr(continuation_module, "_run_continuation", _block)
    assert await ops.answer_interaction("d1", "go") == {"interaction_id": "d1", "status": "answered"}
    await _drain()

    due = await wired.store.claim_continuation_retry(
        wired.fake, "d1", now + timedelta(hours=2), backoff_base_ms=1000, backoff_cap_ms=10000
    )
    assert isinstance(due, ContinuationDue)
    assert due.deferred_binding is not None
    assert due.run_input == {"x": 5, "n": 4}
    assert due.door_id == "pp"
    release.set()
    await _drain()


async def test_reaper_redelivery_applies_the_deferred_binding(monkeypatch, fake_redis):
    # The async-park path: the reaper's detached redelivery drives the stored continuation to its
    # terminal and applies the deferred updates — read off the self-contained continuation-due record.
    wired = _wire(monkeypatch, fake_redis)
    fake_states = _FakeStates(record={"n": 4})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake_states.patch_onto(app._states_facet, monkeypatch)
            monkeypatch.setattr(continuation_module, "_run_continuation", _terminal_stub({"ok": 9}))
            due = ContinuationDue(
                interaction_id="rd1",
                tool="resume_tool",
                identity="svc-key",
                fingerprint="fp-1",
                answer="go",
                attempts=0,
                run_delivery_id="rd-9",
                deferred_binding=_door_binding(),
                run_input={"x": 5, "n": 4},
                door_id="pp",
            )
            continuation_module.redeliver_continuation(wired.store, due)
            await _drain()
            assert ("__custom__", [{"op": "set", "path": ["last"], "value": 9}]) in fake_states.applies

    await run()


async def test_failed_terminal_applies_no_deferred_updates(monkeypatch, fake_redis):
    # A run that reaches a FAILED terminal applies NO updates — exactly as the live apply runs only
    # on a clean output. The deferred binding is carried but never fired on a ``RunTerminalFailed``.
    wired = _wire(monkeypatch, fake_redis)
    fake_states = _FakeStates(record={"n": 4})

    async def _raise_failed(*args, **kwargs):
        raise RunTerminalFailed({"boom": True})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake_states.patch_onto(app._states_facet, monkeypatch)
            monkeypatch.setattr(continuation_module, "_run_continuation", _raise_failed)
            due = ContinuationDue(
                interaction_id="rf1",
                tool="resume_tool",
                identity="svc-key",
                fingerprint="fp-1",
                answer="go",
                attempts=0,
                run_delivery_id="rd-f",
                deferred_binding=_door_binding(),
                run_input={"x": 5, "n": 4},
                door_id="pp",
            )
            continuation_module.redeliver_continuation(wired.store, due)
            await _drain()
            assert fake_states.applies == []

    await run()


async def test_person_erase_scrubs_the_stored_run_input(monkeypatch, fake_redis):
    # The run input the park stores may hold person data; a person erase whole-chain-kills the park,
    # deleting the state hash (the full request, run input included) — so the erase scrubs it.
    wired = _wire(monkeypatch, fake_redis)
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=1)
    from tai42_contract.states import StateContext, SubjectCandidates

    candidates = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "alice"})
    ctx = StateContext(door="api", candidates=candidates)
    req = InteractionRequest(
        interaction_id="pe1",
        group_id="pg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("pe1"),
        created_at=now,
        timeout_at=deadline,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=deadline,
        on_expiry="resume",
        run_delivery_id="rd-pe",
        continuation_state_context=ctx,
        deferred_binding=_door_binding(),
        run_input={"ssn": "secret-person-data"},
        door_id="pp",
    )
    await wired.store.add(
        wired.fake,
        req,
        idle_ttl=86400,
        continuation_fingerprint="fp-1",
        run_delivery_id="rd-pe",
        delivery=None,
    )
    # The run input is stored on the state hash before the erase.
    state = await wired.store.get_state(wired.fake, "pe1")
    assert state is not None
    assert state.request.run_input == {"ssn": "secret-person-data"}
    raw = await wired.fake.hget(wired.store.state_key("pe1"), "run_input")
    assert raw is not None

    reached = await helper_module.cancel_parks_for_person("alice")
    assert "pe1" in reached
    # The state hash is gone — the run input left with it.
    assert await wired.store.get_state(wired.fake, "pe1") is None
    assert await wired.fake.hget(wired.store.state_key("pe1"), "run_input") is None


async def test_re_park_carries_the_deferred_binding_to_the_final_terminal(monkeypatch, fake_redis):
    # A bound run that asks TWICE: the resume of the first park asks AGAIN, so the deferred binding
    # must be carried across the re-park (exactly as the delivery address is) — else the second park
    # stores no binding and the door's update is silently lost at the final terminal. The door update
    # must land at the FINAL terminal, once.
    wired = _wire(monkeypatch, fake_redis)
    fake_states = _FakeStates(record={"n": 4})
    deadline = datetime.now(UTC) + timedelta(hours=1)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake_states.patch_onto(app._states_facet, monkeypatch)
            binding = _door_binding()
            asked = {"again": False}

            async def _reask_then_finish(
                identity,
                fingerprint,
                tool,
                interaction_id,
                answer,
                park_context=None,
                park_asked_by=(),
                caller_ask_landing=None,
                chain_keys=(),
                *,
                mark_detached=True,
            ):
                # The first resume asks AGAIN (a re-park) under the SAME run; the second returns a
                # clean terminal. The re-ask captures whatever binding the drive re-established.
                if not asked["again"]:
                    asked["again"] = True
                    return await helper_module.ask("again?", mode="async", expiry_at=deadline)
                return {"ok": 13}

            monkeypatch.setattr(continuation_module, "_run_continuation", _reask_then_finish)

            tokens = _bind_driver()
            try:
                # Drive the first park: its resume asks again, producing the second park.
                park_b = await continuation_module.drive_and_deliver(
                    wired.store,
                    identity="svc-key",
                    fingerprint="fp-1",
                    tool="resume_tool",
                    interaction_id="rpA",
                    answer="go1",
                    park_context=None,
                    park_asked_by=[],
                    delivery=None,
                    run_delivery_id="rd-rp",
                    candidates=None,
                    receives_outcome=False,
                    deferred_binding=binding,
                    run_input={"x": 5},
                    door_id="pp",
                )
                assert isinstance(park_b, SuspendedInteraction)
                # The re-park carried the per-run binding forward, keyed on the SAME run delivery
                # identity (so the final apply dedups across the re-park).
                state_b = await wired.store.get_state(wired.fake, park_b.interaction_id)
                assert state_b is not None
                assert state_b.request.deferred_binding is not None
                assert state_b.request.run_input == {"x": 5}
                assert state_b.request.door_id == "pp"
                assert state_b.request.run_delivery_id == "rd-rp"

                # Drive the second park to its clean terminal, off its OWN stored fields — the door
                # update lands once at the final terminal.
                await continuation_module.drive_and_deliver(
                    wired.store,
                    identity="svc-key",
                    fingerprint="fp-1",
                    tool="resume_tool",
                    interaction_id=park_b.interaction_id,
                    answer="go2",
                    park_context=None,
                    park_asked_by=[],
                    delivery=None,
                    run_delivery_id=state_b.request.run_delivery_id,
                    candidates=None,
                    receives_outcome=False,
                    deferred_binding=state_b.request.deferred_binding,
                    run_input=state_b.request.run_input,
                    door_id=state_b.request.door_id,
                )
            finally:
                _unbind_driver(tokens)

            assert ("__custom__", [{"op": "set", "path": ["last"], "value": 13}]) in fake_states.applies

    await run()
