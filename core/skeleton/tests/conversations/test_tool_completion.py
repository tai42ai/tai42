"""The resumed tool-outcome delivery tool and the completion-context binding a parked turn makes."""

from __future__ import annotations

import logging

import pytest
from tai42_contract.agent import Agent
from tai42_contract.interactions import PARK_COMPLETION_FAILED, PARK_COMPLETION_SUCCEEDED

from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import completion_delivery as completion_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import record as record_module
from tai42_skeleton.runs.chokepoint import delivery_fire

from .conftest import (
    _TURN_LOGGER,
    FakeChannel,
    FakeManager,
    _channel_route,
    _completion_warnings,
    _EchoInput,
    _settle,
    _store,
    _tool_channel_route,
    _wire,
    _wire_tool,
)


async def _fire_tool(**kw):
    """Fire ``deliver_tool_completion`` inside the platform's delivery-fire context, as the ladder does.

    The real delivery ladder wraps every address-tool fire in ``delivery_fire(completion_id)``; a
    test firing the tool directly reproduces that so the tool's delivery-authorisation guard passes. A
    missing/blank id is fired WITHOUT a context (the ladder never fires an id-less completion), so the
    guard refuses it.
    """
    cid = kw.get("completion_id")
    if not isinstance(cid, str) or not cid:
        return await turn_module.deliver_tool_completion(**kw)
    with delivery_fire(cid):
        return await turn_module.deliver_tool_completion(**kw)


async def _fire_agent(**kw):
    """Fire ``deliver_agent_completion`` inside the platform's delivery-fire context, as the ladder does."""
    cid = kw.get("completion_id")
    if not isinstance(cid, str) or not cid:
        return await turn_module.deliver_agent_completion(**kw)
    with delivery_fire(cid):
        return await turn_module.deliver_agent_completion(**kw)


async def test_tool_route_park_binds_completion_and_delivers_via_reply_expr(env, monkeypatch):
    # A conversation route to a GENERIC parking tool binds the generic tool-route
    # completion (naming THIS thread as the opaque delivery address) around the dispatch and
    # parks silently; when the tool's own resumer later fires deliver_tool_completion with the
    # terminal outcome, the route's reply_expr maps it and it is delivered back into the thread,
    # idempotent on completion_id.
    from tai42_contract.interactions import SuspendedInteraction, get_park_completion

    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    bound: list = []

    def _park(kw):
        bound.append(get_park_completion())
        return SuspendedInteraction(interaction_id="i-async")

    _wire_tool(monkeypatch, _park)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    # The dispatch parked silently; the completion was bound naming deliver_tool_completion and
    # this turn's thread as the opaque delivery address.
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []
    assert len(bound) == 1
    tool_name, context = bound[0]
    assert tool_name == turn_module.DELIVER_TOOL_COMPLETION_NAME
    thread_id = context["delivery_thread_id"]
    assert thread_id == "bridge:tool-line:+15550002222"
    # The ORIGINATING route is pinned into the completion context so the resumed outcome maps
    # through THIS route's reply_expr, not the route a linked person last wrote from.
    assert context["route_name"] == "tool-line"

    # The resumer drives to a clean terminal out of band and fires the completion; reply_expr
    # maps the terminal outcome and it is delivered back into the thread.
    out = await _fire_tool(
        delivery_thread_id=thread_id,
        completion_id="c1",
        result={"result": {"reply": "the deferred answer"}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out == {"message_id": "c1"}
    delivered = await _store().get_record("c1")
    assert delivered is not None
    assert delivered.answer == "the deferred answer"
    assert delivered.delivery_status is DeliveryStatus.DELIVERED
    assert "the deferred answer" in [n.message for n in channel.sends]

    # Idempotent: a redelivered fire under the same completion_id delivers nothing new.
    sends_before = len(channel.sends)
    out2 = await _fire_tool(
        delivery_thread_id=thread_id,
        completion_id="c1",
        result={"result": {"reply": "SECOND"}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out2 == {"message_id": "c1"}
    assert len(channel.sends) == sends_before


async def test_deliver_tool_completion_non_success_delivers_error_notice(env, monkeypatch, caplog):
    # A non-success terminal (the route carries no error mapping) delivers the uniform
    # client-safe notice — the DELIVERY route's ``error_reply_text`` when set, never the raw
    # internal detail, never a silent drop. The participant-facing notice resolves through the
    # delivery route (where the record is filed and the participant is conversing), NOT the pinned
    # originating route — so an originating route carrying a DIFFERENT error_reply_text does
    # not win here (pinning the deliberate choice at turn.py:1608).
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    other = "This originating-route text must NOT win."
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null", error_reply_text=spanish)
    origin = _tool_channel_route(route_name="tool-origin", reply_expr=".x", error_reply_text=other)
    _wire(monkeypatch, FakeManager(route, origin), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire_tool(
            delivery_thread_id="bridge:tool-line:+15550002222",
            completion_id="e1",
            result={"detail": "internal"},
            status=PARK_COMPLETION_FAILED,
            route_name="tool-origin",
        )
    await _settle()
    assert out == {"message_id": "e1"}
    rec = await _store().get_record("e1")
    assert rec is not None
    # The delivery route's custom text wins over the originating route's.
    assert rec.answer == spanish
    assert [n.message for n in channel.sends] == [spanish]
    # An EXPLICIT failure is announced: this record is indistinguishable from a degraded one,
    # so a fleet whose resumes are all failing must still be visible in the log.
    warnings = _completion_warnings(caplog, "e1")
    assert len(warnings) == 1
    assert "'failed'" in warnings[0]


async def test_deliver_tool_completion_unmappable_success_delivers_error_notice(env, monkeypatch):
    # A success terminal the route's reply_expr cannot map is delivered as the client-safe
    # notice rather than crashing the resumer or dropping the outcome.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    channel = FakeChannel()
    # a non-string/non-null emit raises; the delivery route carries the custom notice.
    route = _tool_channel_route(reply_expr=".result.reply", error_reply_text=spanish)
    _wire(monkeypatch, FakeManager(route), channel)

    out = await _fire_tool(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="u1",
        result={"result": {"reply": {"not": "a string"}}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out == {"message_id": "u1"}
    rec = await _store().get_record("u1")
    assert rec is not None
    # The delivery route's custom notice, not the built-in default.
    assert rec.answer == spanish


async def test_deliver_tool_completion_silent_reply_delivers_nothing(env, monkeypatch):
    # A success whose reply_expr maps to null is a designed silent outcome: nothing delivered,
    # and — naturally idempotent — no record anchors it.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    out = await _fire_tool(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="s1",
        result={"result": {}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out == {"message_id": None}
    assert await _store().get_record("s1") is None
    assert channel.sends == []


async def test_deliver_tool_completion_unresolvable_thread_raises(env, monkeypatch):
    # An unresolvable delivery address raises loudly so the resumer's at-least-once seam
    # retains and retries rather than dropping the outcome. PRESENT but unresolvable is the
    # retriable case — a route can be restored — unlike an ABSENT address, which is dropped.
    _wire(monkeypatch, FakeManager(_tool_channel_route()))
    with pytest.raises(turn_module.CompletionDeliveryError):
        await _fire_tool(delivery_thread_id="not-a-bridge-thread", completion_id="x1", result="hi")


async def test_deliver_tool_completion_without_a_completion_id_raises(env, monkeypatch):
    # The idempotency id is the exactly-once key. A key-less (or blank-id) fire is never a
    # legitimate ladder fire — nothing authorized it — so the delivery-authorisation guard refuses
    # it before any mint, never delivering under a guessed id.
    from tai42_contract.interactions import ParkDeliveryUnauthorizedError

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_expr=".result.reply // null")), channel)

    for missing in (None, ""):
        with pytest.raises(ParkDeliveryUnauthorizedError):
            await _fire_tool(
                delivery_thread_id="bridge:tool-line:+15550002222",
                completion_id=missing,
                result={"result": {"reply": "hi"}},
                status=PARK_COMPLETION_SUCCEEDED,
            )
    await _settle()
    assert channel.sends == []


async def test_deliver_tool_completion_without_an_address_is_a_logged_no_op(env, monkeypatch, caplog):
    # A fire whose completion binding carried NO address: nothing reverses a completion id to a
    # thread, so it is a LOGGED drop — never the retriable CompletionDeliveryError, which would
    # buy an unending retry storm against a fire no attempt can ever land.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_expr=".result.reply // null")), channel)

    with caplog.at_level(logging.ERROR, logger=_TURN_LOGGER):
        out = await _fire_tool(
            delivery_thread_id=None,
            completion_id="orphan-1",
            result={"result": {"reply": "the orphaned outcome"}},
            status=PARK_COMPLETION_SUCCEEDED,
        )
        blank = await _fire_tool(
            delivery_thread_id="",
            completion_id="orphan-2",
            result={"result": {"reply": "also orphaned"}},
            status=PARK_COMPLETION_SUCCEEDED,
        )
    await _settle()

    assert out == {"message_id": None}
    assert blank == {"message_id": None}
    assert await _store().get_record("orphan-1") is None
    assert await _store().get_record("orphan-2") is None
    assert channel.sends == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and r.name == _TURN_LOGGER]
    assert any("orphan-1" in message for message in errors)
    assert any("orphan-2" in message for message in errors)


async def test_deliver_tool_completion_omitted_status_delivers_notice_and_warns(env, monkeypatch, caplog):
    # FAIL-SAFE: an omitted status takes this tool's PUBLISHED default (FAILED), so it delivers
    # the client-safe notice and NEVER pushes an unmapped payload through reply_expr as if it had
    # succeeded. The fire still returns a delivered record, so pairing a new skeleton with an old
    # resumer degrades EVERY outcome to a notice — the WARNING is the only detection there is.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire_tool(
            delivery_thread_id="bridge:tool-line:+15550002222",
            completion_id="d1",
            result={"result": {"reply": "would-map-if-success"}},
        )
    await _settle()
    assert out == {"message_id": "d1"}
    rec = await _store().get_record("d1")
    assert rec is not None
    # Delivered the notice, NOT the reply_expr-mapped success text.
    assert rec.answer == outcome_module._ERROR_ANSWER_TEXT
    warnings = _completion_warnings(caplog, "d1")
    assert len(warnings) == 1
    assert "'failed'" in warnings[0]


async def test_deliver_tool_completion_explicit_none_status_warns_as_unstamped(env, monkeypatch, caplog):
    # A resumer that puts ``status: None`` ON THE WIRE is an unstamped fire, and is reported as
    # one — the shape is distinguishable from a terminal that genuinely failed, which is what
    # makes the log usable for telling a version skew apart from a failing fleet.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire_tool(
            delivery_thread_id="bridge:tool-line:+15550002222",
            completion_id="n1",
            result={"result": {"reply": "would-map-if-success"}},
            status=None,
        )
    await _settle()
    assert out == {"message_id": "n1"}
    rec = await _store().get_record("n1")
    assert rec is not None
    assert rec.answer == outcome_module._ERROR_ANSWER_TEXT
    warnings = _completion_warnings(caplog, "n1")
    assert len(warnings) == 1
    assert "NO status" in warnings[0]


async def test_deliver_tool_completion_unrecognized_status_delivers_notice_and_warns(env, monkeypatch, caplog):
    # A status outside the contract vocabulary is non-success, exactly like the unstamped fire:
    # a value this tool cannot read must never be mapped as though it were the success terminal.
    # Same invisible version skew, so it warns and names the value that arrived.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire_tool(
            delivery_thread_id="bridge:tool-line:+15550002222",
            completion_id="w1",
            result={"result": {"reply": "would-map-if-success"}},
            status="weird",
        )
    await _settle()
    assert out == {"message_id": "w1"}
    rec = await _store().get_record("w1")
    assert rec is not None
    assert rec.answer == outcome_module._ERROR_ANSWER_TEXT
    warnings = _completion_warnings(caplog, "w1")
    assert len(warnings) == 1
    assert "'weird'" in warnings[0]


async def test_deliver_tool_completion_success_maps_the_reply_and_warns_nothing(env, monkeypatch, caplog):
    # The counterpart pin: the ONE recognized success status maps through reply_expr and is
    # silent, so the warnings above discriminate a degraded fire from a healthy one.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire_tool(
            delivery_thread_id="bridge:tool-line:+15550002222",
            completion_id="ok1",
            result={"result": {"reply": "the deferred answer"}},
            status=PARK_COMPLETION_SUCCEEDED,
        )
    await _settle()
    assert out == {"message_id": "ok1"}
    rec = await _store().get_record("ok1")
    assert rec is not None
    assert rec.answer == "the deferred answer"
    assert _completion_warnings(caplog, "ok1") == []


async def test_deliver_tool_completion_maps_via_the_pinned_originating_route(env, monkeypatch):
    # A park started under route A; the linked person then wrote from route B, so
    # _resolve_completion_target reverses the thread to route B (where they last wrote). The
    # completion must still map the terminal through the ORIGINATING route A's reply_expr — not
    # route B's, which would map the SAME result wrongly. Delivery still lands on route B's
    # address (where the person is), but the mapped text comes from A.
    channel = FakeChannel()
    route_a = _tool_channel_route(route_name="tool-a", reply_expr=".a.text")
    route_b = _tool_channel_route(route_name="tool-b", our_identity="+15550009999", reply_expr=".b.text")
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)

    # Simulate the linked person having last written from route B: the thread reverses to B.
    async def _resolve_to_b(_thread_id):
        return route_b, "+15550002222"

    monkeypatch.setattr(completion_module, "_resolve_completion_target", _resolve_to_b)

    out = await _fire_tool(
        delivery_thread_id="bridge:@person:PID1",
        completion_id="p1",
        result={"a": {"text": "via-A"}, "b": {"text": "via-B"}},
        status=PARK_COMPLETION_SUCCEEDED,
        route_name="tool-a",
    )
    await _settle()
    assert out == {"message_id": "p1"}
    rec = await _store().get_record("p1")
    assert rec is not None
    # Mapped through route A's reply_expr (the originating route), NOT route B's.
    assert rec.answer == "via-A"
    # Delivered where the person last wrote (route B's reversed address).
    assert rec.route_name == "tool-b"


async def test_deliver_tool_completion_raises_on_vanished_originating_route(env, monkeypatch):
    # The pinned originating route no longer exists (deleted between park and resume): raise
    # loudly so the resumer retries rather than mapping through the wrong route.
    channel = FakeChannel()
    route = _tool_channel_route(route_name="tool-live", reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    with pytest.raises(turn_module.CompletionDeliveryError):
        await _fire_tool(
            delivery_thread_id="bridge:tool-live:+15550002222",
            completion_id="v1",
            result={"result": {"reply": "x"}},
            status=PARK_COMPLETION_SUCCEEDED,
            route_name="tool-gone",
        )


async def test_agent_route_park_binds_the_thread_as_the_completion_context(env, monkeypatch):
    # The AGENT-kind mirror of the tool-route leg: a route whose AGENT async-parks binds the
    # completion naming THIS thread in the OPAQUE context, keyed by the delivery tool's own
    # routing parameter. A resuming driver fires the generic contract payload
    # ``{**context, result, completion_id, status}``, so a context-less binding would fire a
    # payload the delivery tool cannot accept and nothing would be delivered.
    from tai42_contract.agent.events import SuspendedFinal
    from tai42_contract.interactions import get_park_completion

    bound: list = []

    class ParkingAgent(Agent):
        tool_name = "echo"
        ToolInput = _EchoInput

        async def run(self, **kwargs):
            raise AssertionError("the turn drives astream")

        async def astream(self, **kwargs):
            bound.append(get_park_completion())
            yield SuspendedFinal(interaction_ids=["i-async"], thread_id=kwargs["thread_id"])

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": ParkingAgent()})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    # The turn parked silently — no reply now; the resumed answer arrives out of band.
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []

    assert len(bound) == 1
    tool_name, context = bound[0]
    assert tool_name == turn_module.COMPLETION_TOOL_NAME
    assert context is not None
    # The completion context carries this turn's thread AND its intake ``message_id``, so the
    # late-path deliverer can rebuild ``$turn`` from the stored record.
    assert dict(context) == {"thread_id": "bridge:line:+15550002222", "message_id": message_id}

    # The contract payload a resuming driver fires — the bound context merged with the terminal
    # outcome — is exactly what the bound tool accepts, and it delivers the answer back.
    out = await _fire_agent(
        **context, result="the deferred answer", completion_id="ac1", status=PARK_COMPLETION_SUCCEEDED
    )
    await _settle()
    assert out == {"message_id": "ac1"}
    delivered = await _store().get_record("ac1")
    assert delivered is not None
    assert delivered.answer == "the deferred answer"
    assert [n.message for n in channel.sends] == ["the deferred answer"]


async def test_completion_tools_refuse_outside_the_delivery_fire(env, monkeypatch):
    # an address tool delivers ONLY inside the platform's delivery-fire context for its own
    # completion. Named at the run-tool door or the MCP edge (no ambient fire), BOTH tools refuse a
    # well-formed fire before any record read or mint; from the ladder's context the tool delivers.
    from tai42_contract.interactions import ParkDeliveryUnauthorizedError

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_expr=".result.reply // null")), channel)

    with pytest.raises(ParkDeliveryUnauthorizedError):
        await turn_module.deliver_tool_completion(
            delivery_thread_id="bridge:tool-line:+15550002222",
            completion_id="c-unauth",
            result={"result": {"reply": "hi"}},
            status=PARK_COMPLETION_SUCCEEDED,
        )
    with pytest.raises(ParkDeliveryUnauthorizedError):
        await turn_module.deliver_agent_completion(
            thread_id="bridge:tool-line:+15550002222",
            completion_id="c-unauth-2",
            result="hi",
            status=PARK_COMPLETION_SUCCEEDED,
        )
    assert channel.sends == []

    out = await _fire_tool(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="c-ok",
        result={"result": {"reply": "delivered once"}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out == {"message_id": "c-ok"}
    assert [n.message for n in channel.sends] == ["delivered once"]


async def test_deliver_tool_completion_reply_reads_turn_rebuilt_from_the_intake_record(env, monkeypatch):
    # the late path rebuilds ``$turn`` from the originating intake record the completion
    # binding's ``message_id`` names, so a resumed reply reads ``$turn`` symmetric to a live turn;
    # a record that has aged out reads ``$turn`` as null.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr='$turn.id // "no-turn"')
    _wire(monkeypatch, FakeManager(route), channel)
    intake = record_module._new_record(
        route=route,
        message_id="intake-1",
        thread_id="bridge:tool-line:+15550002222",
        client_address="+15550002222",
        caller_principal=None,
        provider_message_id="PID1",
        inbound_text="hi",
        delivery_status=DeliveryStatus.SILENT,
    )
    await _store().create_record(intake)

    out = await _fire_tool(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="c-turn",
        result={"x": 1},
        status=PARK_COMPLETION_SUCCEEDED,
        message_id="intake-1",
    )
    await _settle()
    assert out == {"message_id": "c-turn"}
    delivered = await _store().get_record("c-turn")
    assert delivered is not None
    assert delivered.answer == "intake-1"

    # A gone (absent) record → ``$turn`` is null → the reply's own fallback.
    out2 = await _fire_tool(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="c-gone",
        result={"x": 1},
        status=PARK_COMPLETION_SUCCEEDED,
        message_id="does-not-exist",
    )
    await _settle()
    assert out2 == {"message_id": "c-gone"}
    gone = await _store().get_record("c-gone")
    assert gone is not None
    assert gone.answer == "no-turn"
