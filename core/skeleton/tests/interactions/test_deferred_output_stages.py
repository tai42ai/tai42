"""The output stages a park captured, applied where each preset's result reappears after the resume.

An answer stage maps the answer before the driver's continuation receives it; its refusal ends the
park through the driver's give-up handler, or FAILED with none. A terminal stage maps the resumed
drive's terminal before the deferred binding reads it and before it is delivered; its refusal is
the run's FAILED. A chained stage settles a chained run's terminal before it fires along its
routing. The stages ride the stored request, the answer claim and the continuation-due record, so
every door that drives a park applies them once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    AnswerFormat,
    DeliveryAddress,
    InteractionRequest,
    InteractionResponse,
    RunFailed,
    RunTerminalFailed,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.presets import PresetOutputStage, PresetParkStages, preset_output_failed_outcome
from tai42_contract.tools import RunDelivery, run_delivery, tool_call_frame
from tai42_kit.interactions import park_giveup
from tai42_kit.interactions.park_giveup import ParkGiveUpFailure, ParkGiveUpOutcome, register_park_giveup_handler

from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import ask, giveup_delivery
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import continuation_dispatch as continuation_dispatch_module
from tai42_skeleton.interactions.store import ContinuationDue
from tai42_skeleton.tools.stage_frames import (
    drive_output_stages,
    run_stage_scope,
    settle_chained_terminal,
    stage_frame,
)

from ._continuation_support import configure_interactions_store, make_wired

KEY = "tai42:chained-park:k"
MAP_FAILED = "preset 'p' output map failed"
ADDRESS = DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1")


def _failing(preset: str = "p") -> PresetOutputStage:
    return PresetOutputStage(preset=preset, output_map='error("no")')


def _mapping(output_map: str, preset: str = "p") -> PresetOutputStage:
    return PresetOutputStage(preset=preset, output_map=output_map)


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_interactions_store(monkeypatch)


@pytest.fixture(autouse=True)
def _isolated_giveup_handlers() -> Iterator[None]:
    saved = list(park_giveup._park_giveup_handlers)
    park_giveup._park_giveup_handlers.clear()
    yield
    park_giveup._park_giveup_handlers[:] = saved


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, fake_redis: Any, fake_client_ctx: Any) -> SimpleNamespace:
    w = make_wired(monkeypatch, fake_redis, fake_client_ctx)

    @asynccontextmanager
    async def _bind(identity: str, *, bound_fingerprint: str = ""):
        token = set_execution_identity(CallerIdentity(user_id=identity, execution_key_fingerprint=bound_fingerprint))
        try:
            yield
        finally:
            reset_execution_identity(token)

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _bind)
    return w


class _Tools:
    """A ``run_tool`` fake that dispatches by name and records each dispatch."""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False, continues_chain=None):
        self.calls.append({"key": key, "arguments": dict(arguments)})
        handler = self.handlers.get(key)
        return None if handler is None else handler(arguments)

    def arguments_of(self, key: str) -> list[dict[str, Any]]:
        return [call["arguments"] for call in self.calls if call["key"] == key]


def _wire_tools(monkeypatch: pytest.MonkeyPatch, handlers: dict[str, Any]) -> _Tools:
    tools = _Tools({"deliver_tool": lambda _a: None, **handlers})
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=tools))
    return tools


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _apply(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(continuation_module, "apply_deferred_binding", _apply)
    return calls


async def _seed_due(wired: SimpleNamespace, interaction_id: str) -> None:
    await wired.fake.hset(
        wired.store.continuation_due_key(interaction_id),
        mapping={
            "tool": "resume_tool",
            "identity": "svc-key",
            "fingerprint": "fp-1",
            "answer": "null",
            "attempts": "0",
        },
    )
    await wired.fake.zadd(wired.store.continuation_due_index_key, {interaction_id: 0})


async def _drive(
    wired: SimpleNamespace,
    stages: PresetParkStages | None,
    *,
    answer: Any = {"a": 1},  # noqa: B006 (never mutated)
    receives_outcome: bool = False,
) -> Any:
    if not receives_outcome:
        await _seed_due(wired, "iid-1")
    return await continuation_module.drive_and_deliver(
        wired.store,
        identity="svc-key",
        fingerprint="fp-1",
        tool="resume_tool",
        interaction_id="iid-1",
        answer=answer,
        reply_to_medium_message_id=None,
        params=None,
        park_context=None,
        park_asked_by=("main",),
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
        candidates=None,
        receives_outcome=receives_outcome,
        deferred_binding=None,
        output_stages=stages,
    )


def _delivered(tools: _Tools) -> dict[str, Any]:
    (fire,) = tools.arguments_of("deliver_tool")
    return fire


# --- the answer stage ------------------------------------------------------------------------------


async def test_an_answer_stage_maps_the_ask_result_the_continuation_receives(wired, monkeypatch) -> None:
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda a: a["ask_result"]})
    await _drive(wired, PresetParkStages(answer=[_mapping(".answer.a")]))
    assert tools.arguments_of("resume_tool")[0]["ask_result"] == 1
    assert _delivered(tools)["result"] == 1


async def test_an_answer_stage_skips_the_expiry_answer(wired, monkeypatch) -> None:
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda a: "expired"})
    await _drive(wired, PresetParkStages(answer=[_failing()]), answer=EXPIRY_ANSWER)
    assert tools.arguments_of("resume_tool")[0]["ask_result"] == {
        "answer": EXPIRY_ANSWER,
        "reply_to": None,
        "params": None,
    }


async def test_an_answer_stage_refusal_with_no_handler_delivers_failed(wired, monkeypatch) -> None:
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda a: "never"})
    await _drive(wired, PresetParkStages(answer=[_failing()]))
    assert tools.arguments_of("resume_tool") == []
    fire = _delivered(tools)
    assert fire["status"] == PARK_COMPLETION_FAILED
    assert fire["result"] == preset_output_failed_outcome("p", MAP_FAILED)
    assert "reply" not in fire


async def test_an_answer_stage_refusal_hands_the_handler_the_typed_failure(wired, monkeypatch) -> None:
    _wire_tools(monkeypatch, {})
    seen: list[ParkGiveUpFailure] = []

    async def _handler(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome:
        seen.append(failure)
        return ParkGiveUpOutcome(RunFailed(outcome=dict(failure.outcome)))

    register_park_giveup_handler(_handler)
    await _drive(wired, PresetParkStages(answer=[_failing()]))
    assert seen == [
        ParkGiveUpFailure(
            preset_output_failed_outcome("p", MAP_FAILED),
            MAP_FAILED,
            "PresetOutputError",
            ErrorKind.UPSTREAM_ERROR,
            False,
        )
    ]


def _replying_handler() -> None:
    async def _handler(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome:
        return ParkGiveUpOutcome(RunFailed(outcome={"handled": True}, reply="The run could not go on."))

    register_park_giveup_handler(_handler)


async def test_a_handlers_failure_reaches_a_live_receiver_with_its_reply_and_no_deferred_binding(
    wired, monkeypatch, applied
) -> None:
    _wire_tools(monkeypatch, {})
    _replying_handler()
    with pytest.raises(RunTerminalFailed) as raised:
        await _drive(wired, PresetParkStages(answer=[_failing()]), receives_outcome=True)
    assert raised.value.outcome == {"handled": True}
    assert raised.value.reply == "The run could not go on."
    assert applied == []


async def test_a_handlers_failure_is_delivered_failed_with_its_reply_without_a_receiver(
    wired, monkeypatch, applied
) -> None:
    tools = _wire_tools(monkeypatch, {})
    _replying_handler()
    await _drive(wired, PresetParkStages(answer=[_failing()]))
    fire = _delivered(tools)
    assert fire["status"] == PARK_COMPLETION_FAILED
    assert fire["result"] == {"handled": True}
    assert fire["reply"] == "The run could not go on."
    assert applied == []


async def test_a_handlers_success_after_an_answer_stage_refusal_passes_the_terminal_stage(wired, monkeypatch) -> None:
    tools = _wire_tools(monkeypatch, {})

    async def _handler(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome:
        return ParkGiveUpOutcome("recovered")

    register_park_giveup_handler(_handler)
    await _drive(wired, PresetParkStages(answer=[_failing()], terminal=[_mapping("{v: .}", "door")]))
    fire = _delivered(tools)
    assert fire["status"] == PARK_COMPLETION_SUCCEEDED
    assert fire["result"] == {"v": "recovered"}


# --- the terminal stage ----------------------------------------------------------------------------


async def test_a_mapped_terminal_reaches_the_deferred_binding_and_the_delivery(wired, monkeypatch, applied) -> None:
    from tai42_contract.states import StateAttach, StateBinding
    from tai42_contract.template import TemplatedText

    tools = _wire_tools(monkeypatch, {"resume_tool": lambda a: {"raw": 3}})
    binding = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(content=".thread_id"))])
    await _seed_due(wired, "iid-1")
    await continuation_module.drive_and_deliver(
        wired.store,
        identity="svc-key",
        fingerprint="fp-1",
        tool="resume_tool",
        interaction_id="iid-1",
        answer="go",
        reply_to_medium_message_id=None,
        params=None,
        park_context=None,
        park_asked_by=(),
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
        candidates=None,
        receives_outcome=False,
        deferred_binding=binding,
        run_input={"k": "v"},
        door_id="door-1",
        output_stages=PresetParkStages(terminal=[_mapping("{mapped: .raw}")]),
    )
    assert [call["output"] for call in applied] == [{"mapped": 3}]
    assert _delivered(tools)["result"] == {"mapped": 3}


async def test_a_terminal_stage_refusal_is_failed_for_a_live_receiver(wired, monkeypatch, applied) -> None:
    _wire_tools(monkeypatch, {"resume_tool": lambda a: "done"})
    with pytest.raises(RunTerminalFailed) as raised:
        await _drive(wired, PresetParkStages(terminal=[_failing()]), receives_outcome=True)
    assert raised.value.outcome == preset_output_failed_outcome("p", MAP_FAILED)
    assert raised.value.reply is None
    assert applied == []


async def test_a_terminal_stage_refusal_is_delivered_failed_without_a_receiver(wired, monkeypatch, applied) -> None:
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda a: "done"})
    await _drive(wired, PresetParkStages(terminal=[_failing()]))
    fire = _delivered(tools)
    assert fire["status"] == PARK_COMPLETION_FAILED
    assert fire["result"] == preset_output_failed_outcome("p", MAP_FAILED)
    assert applied == []
    assert not await wired.fake.hgetall(wired.store.continuation_due_key("iid-1"))


async def test_a_re_park_passes_the_terminal_stage_untouched(wired, monkeypatch) -> None:
    parked = SuspendedInteraction(interaction_id="iid-next")
    _wire_tools(monkeypatch, {"resume_tool": lambda a: parked})
    result = await _drive(wired, PresetParkStages(terminal=[_failing()]), receives_outcome=True)
    assert result is parked


# --- the chained stage -----------------------------------------------------------------------------


async def test_settle_applies_the_stages_of_its_own_chain_key() -> None:
    stages = PresetParkStages(chained={KEY: [_mapping("{wrapped: .}")]})
    with drive_output_stages(stages):
        assert await settle_chained_terminal(KEY, 5) == {"wrapped": 5}
        assert await settle_chained_terminal("tai42:chained-park:other", 5) == 5
    assert await settle_chained_terminal(KEY, 5) == 5


async def test_settle_passes_a_run_failed_through_and_turns_a_refusal_into_one() -> None:
    failed = RunFailed(outcome={"x": 1})
    with drive_output_stages(PresetParkStages(chained={KEY: [_failing()]})):
        assert await settle_chained_terminal(KEY, failed) is failed
        refused = await settle_chained_terminal(KEY, "done")
    assert refused == RunFailed(outcome=preset_output_failed_outcome("p", MAP_FAILED))


async def test_the_interactions_facet_settles_through_the_drive_stages() -> None:
    from tai42_skeleton.app.instance import app

    with drive_output_stages(PresetParkStages(chained={KEY: [_mapping(". + 1")]})):
        assert await app.interactions.settle_chained_terminal(KEY, 1) == 2


async def test_a_chained_fire_inside_a_drive_is_settled_by_that_drives_stages(wired, monkeypatch) -> None:
    seen: list[Any] = []

    async def _run_tool(key: str, arguments: dict[str, Any], *, offload_sync: bool = False, continues_chain=None):
        if key == "resume_tool":
            seen.append(await settle_chained_terminal(KEY, 1))
            return "done"
        return None

    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=SimpleNamespace(run_tool=_run_tool)))
    await _drive(wired, PresetParkStages(chained={KEY: [_mapping(". * 10")]}))
    assert seen == [10]


# --- the give-up path ------------------------------------------------------------------------------


def _request(stages: PresetParkStages | None) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id="iid-g",
        group_id="gg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to="reply:iid-g",
        created_at=now,
        timeout_at=now + timedelta(hours=1),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=now + timedelta(hours=1),
        run_delivery_id="rd-g",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="tg"),
        deferred_output_stages=stages,
    )


async def test_a_give_up_success_passes_the_terminal_stage(wired, monkeypatch) -> None:
    tools = _wire_tools(monkeypatch, {})

    async def _handler(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome:
        return ParkGiveUpOutcome("the caller's answer")

    register_park_giveup_handler(_handler)
    stages = PresetParkStages(terminal=[_mapping("{v: .}")])
    await giveup_delivery.deliver_park_giveup(wired.store, _request(stages), fingerprint=None)
    fire = _delivered(tools)
    assert fire["status"] == PARK_COMPLETION_SUCCEEDED
    assert fire["result"] == {"v": "the caller's answer"}


async def test_a_give_up_success_the_terminal_stage_refuses_is_delivered_failed(wired, monkeypatch) -> None:
    tools = _wire_tools(monkeypatch, {})

    async def _handler(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome:
        return ParkGiveUpOutcome("the caller's answer")

    register_park_giveup_handler(_handler)
    await giveup_delivery.deliver_park_giveup(
        wired.store, _request(PresetParkStages(terminal=[_failing()])), fingerprint=None
    )
    fire = _delivered(tools)
    assert fire["status"] == PARK_COMPLETION_FAILED
    assert fire["result"] == preset_output_failed_outcome("p", MAP_FAILED)


async def test_a_give_up_handler_reads_the_chained_stages_of_the_park(wired, monkeypatch) -> None:
    _wire_tools(monkeypatch, {})
    seen: list[Any] = []

    async def _handler(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome:
        seen.append(await settle_chained_terminal(KEY, RunFailed(outcome=dict(failure.outcome))))
        return ParkGiveUpOutcome(seen[-1])

    register_park_giveup_handler(_handler)
    stages = PresetParkStages(chained={KEY: [_mapping("{v: .}")]})
    await giveup_delivery.deliver_park_giveup(wired.store, _request(stages), fingerprint=None)
    assert seen == [RunFailed(outcome={"tai42:resume_abandoned": True})]


# --- the stored stages ride every door -------------------------------------------------------------


async def _park_under(wired: SimpleNamespace, stage: PresetOutputStage) -> SuspendedInteraction:
    """Park an ask under a door preset's frame: a terminal stage."""
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    try:
        with tool_call_frame(), run_delivery(RunDelivery("rd-p", ADDRESS)), run_stage_scope(), stage_frame(stage):
            tool_token = set_resume_continuation_tool("resume_tool")
            try:
                parked = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
            finally:
                reset_resume_continuation_tool(tool_token)
    finally:
        reset_execution_identity(id_token)
    assert isinstance(parked, SuspendedInteraction)
    return parked


async def _answer(wired: SimpleNamespace, interaction_id: str, group_id: str) -> None:
    due_ttl, first_attempt_ms = continuation_dispatch_module.continuation_due_timing(wired.settings)
    claimed = await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id=interaction_id, answer=1, answered_by="u-1", answered_at=datetime.now(UTC)),
        group_id,
        3600,
        continuation_due_ttl=due_ttl,
        continuation_first_attempt_at_ms=first_attempt_ms,
    )
    assert claimed


async def test_the_stages_ride_the_request_the_claim_and_the_due_record(wired, monkeypatch) -> None:
    _wire_tools(monkeypatch, {})
    stage = _mapping(". + 1")
    parked = await _park_under(wired, stage)
    state = await wired.store.get_state(wired.fake, parked.interaction_id)
    assert state is not None
    assert state.request.deferred_output_stages == PresetParkStages(terminal=[stage])
    await _answer(wired, parked.interaction_id, state.group_id)
    due = await wired.store.claim_continuation_retry(
        wired.fake, parked.interaction_id, datetime.now(UTC) + timedelta(hours=2), 1000, 60000
    )
    assert isinstance(due, ContinuationDue)
    assert due.deferred_output_stages == PresetParkStages(terminal=[stage])


async def test_a_park_under_no_stage_stores_no_stage_field(wired, monkeypatch) -> None:
    _wire_tools(monkeypatch, {})
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    tool_token = set_resume_continuation_tool("resume_tool")
    try:
        with tool_call_frame():
            parked = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_resume_continuation_tool(tool_token)
        reset_execution_identity(id_token)
    assert isinstance(parked, SuspendedInteraction)
    state_key = wired.store.state_key(parked.interaction_id)
    assert await wired.fake.hget(state_key, "deferred_output_stages") is None
    state = await wired.store.get_state(wired.fake, parked.interaction_id)
    assert state is not None
    await _answer(wired, parked.interaction_id, state.group_id)
    due = await wired.store.claim_continuation_retry(
        wired.fake, parked.interaction_id, datetime.now(UTC) + timedelta(hours=2), 1000, 60000
    )
    assert isinstance(due, ContinuationDue)
    assert due.deferred_output_stages is None


async def test_the_reapers_redelivery_applies_the_stage_once(wired, monkeypatch) -> None:
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda a: a["ask_result"]["answer"]})
    parked = await _park_under(wired, _mapping(". + 1"))
    state = await wired.store.get_state(wired.fake, parked.interaction_id)
    assert state is not None
    await _answer(wired, parked.interaction_id, state.group_id)
    due = await wired.store.claim_continuation_retry(
        wired.fake, parked.interaction_id, datetime.now(UTC) + timedelta(hours=2), 1000, 60000
    )
    assert isinstance(due, ContinuationDue)
    continuation_dispatch_module.redeliver_continuation(wired.store, due)
    while continuation_dispatch_module._CONTINUATION_TASKS:
        await asyncio.gather(*list(continuation_dispatch_module._CONTINUATION_TASKS), return_exceptions=True)
    assert [call["ask_result"]["answer"] for call in tools.arguments_of("resume_tool")] == [1]
    assert [call["result"] for call in tools.arguments_of("deliver_tool")] == [2]
