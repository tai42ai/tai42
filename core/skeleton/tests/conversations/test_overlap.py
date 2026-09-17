"""The overlap chokepoint: the per-route policy every message turn passes at turn start.

The four :class:`OverlapPolicy` combinations across the three scenarios (no turn running, one
running, a burst of three), the settle window, the cancel marker and its watcher, the carried
payload, the ambient turn ref, a yielded tool, and the per-door outcomes — proven with the
turn-test harness's fakes on the channel and API doors.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from tai42_contract.conversations import (
    ConversationRoute,
    ConversationTurnRef,
    OverlapPolicy,
    TurnSupersededError,
    current_conversation_turn,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import overlap as overlap_module
from tai42_skeleton.conversations.turn import record as record_module
from tai42_skeleton.conversations.turn import tool_turn as tool_turn_module

from .conftest import (
    BlockingAgent,
    EchoAgent,
    FakeChannel,
    FakeManager,
    _settle,
    _store,
    _wire,
    _wire_tool,
)

_OUR = "+15550001111"
_ADDR = "+15550002222"


# -- route builders carrying an overlap policy -------------------------------


def _agent_route(policy: OverlapPolicy, *, door: str = "channel", route_name: str = "line") -> ConversationRoute:
    kwargs: dict = {
        "route_name": route_name,
        "door": door,
        "target_kind": "agent",
        "target_name": "echo",
        "execution_key": "svc",
        "execution_key_fingerprint": "fp-1",
        "overlap": policy,
    }
    if door == "channel":
        kwargs |= {"channel": "twilio", "our_identity": _OUR}
    else:
        kwargs |= {"callback_url": "https://cb.example/x", "callback_secret": "sec-1"}
    return ConversationRoute(**kwargs)


def _tool_route(
    policy: OverlapPolicy, *, payload_expr: str | None = None, route_name: str = "tool-line"
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="tool",
        target_name="echo-tool",
        payload_expr=TemplatedText(content=payload_expr) if payload_expr is not None else None,
        execution_key="svc",
        channel="twilio",
        our_identity=_OUR,
        execution_key_fingerprint="fp-1",
        overlap=policy,
    )


async def _accept(text: str, pid: str) -> str:
    return await turn_module.accept("twilio", _OUR, _ADDR, _ADDR, text, pid)


async def _poll_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within the timeout")


def _gate_cancel_marker_reads(monkeypatch) -> asyncio.Event:
    """Hold the cancel watcher's marker reads until the returned event is set.

    The watcher polls the shared cancel marker every poll interval; holding each read until the
    whole burst has been accepted makes the first observed marker name the newest accepted
    message. A scenario whose outcome depends on the burst being accepted before the watcher
    observes the marker is thereby deterministic, without altering what the watcher decides.
    """
    gate = asyncio.Event()
    real = overlap_module._read_cancel_marker

    async def _gated(settings: ConversationsSettings, key: str) -> overlap_module._CancelMarker | None:
        await gate.wait()
        return await real(settings, key)

    monkeypatch.setattr(overlap_module, "_read_cancel_marker", _gated)
    return gate


@pytest.fixture(autouse=True)
def _seed_routes(env):
    """Plant the routing rows the record create checks before writing a thread's indexes, so the
    gather/superseded reads over the thread index see the accepted messages (a real route create
    plants the same row)."""
    for name in ("line", "tool-line", "chat"):
        env.seed_route(name)
    return env


async def _record(message_id: str) -> ConversationRecord:
    record = await _store().get_record(message_id)
    assert record is not None
    return record


# -- the four combinations, one turn already running --------------------------


async def test_continue_one_runs_each_message_as_its_own_turn(env, monkeypatch):
    # The default: three messages, three answered turns, byte-identical to today.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy())), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    ids = [await _accept(t, f"P{t}") for t in ("one", "two", "three")]
    await _settle(timeout=6.0)

    records = [await _record(i) for i in ids]
    assert [r.answer_status for r in records] == ["answered", "answered", "answered"]
    assert [r.answer for r in records] == ["echo: one", "echo: two", "echo: three"]


async def test_continue_all_carries_the_burst_after_the_running_turn(env, monkeypatch):
    # continue + all: message 1 runs alone; 2 and 3 ride ONE later turn as its whole text.
    agent = BlockingAgent()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(deliver="all"))), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    id1 = await _accept("one", "P1")
    await asyncio.wait_for(agent.entered.wait(), 2)  # turn 1 is running
    id2 = await _accept("two", "P2")
    id3 = await _accept("three", "P3")
    agent.release.set()
    await _settle(timeout=6.0)

    r1, r2, r3 = await _record(id1), await _record(id2), await _record(id3)
    assert r1.answer == "echo: one"
    # 2 is the lead of the second turn; 3 is merged into it and its text rides the whole turn.
    assert r2.answer_status == "answered"
    assert r2.answer == "echo: two\n\nthree"
    assert r3.delivery_status is DeliveryStatus.MERGED
    assert r3.successor_id == id2
    assert agent.calls == ["one", "two\n\nthree"]


async def test_cancel_one_supersedes_the_running_and_middle_messages(env, monkeypatch):
    # cancel + one: 1 cancelled, 2 superseded, one turn with 3.
    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(running="cancel"))), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    gate = _gate_cancel_marker_reads(monkeypatch)

    id1 = await _accept("one", "P1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    id2 = await _accept("two", "P2")
    id3 = await _accept("three", "P3")
    gate.set()  # the whole burst is accepted; the first marker the watcher reads names id3

    async def _two_superseded() -> bool:
        r1, r2 = await _record(id1), await _record(id2)
        return r1.delivery_status is DeliveryStatus.SUPERSEDED and r2.delivery_status is DeliveryStatus.SUPERSEDED

    await _poll_until(_two_superseded)
    agent.release.set()
    await _settle(timeout=6.0)

    r1, r2, r3 = await _record(id1), await _record(id2), await _record(id3)
    assert r1.delivery_status is DeliveryStatus.SUPERSEDED
    assert r1.successor_id == id3
    assert r2.delivery_status is DeliveryStatus.SUPERSEDED
    assert r2.successor_id == id3
    assert r3.answer == "echo: three"


async def test_cancel_all_carries_the_cancelled_message_in_superseded(env, monkeypatch):
    # cancel + all: 1 cancelled, one turn carrying 2 and 3 with 1's text in the whole turn.
    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(running="cancel", deliver="all"))), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    gate = _gate_cancel_marker_reads(monkeypatch)

    id1 = await _accept("one", "P1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    id2 = await _accept("two", "P2")
    id3 = await _accept("three", "P3")
    gate.set()  # the whole burst is accepted before the watcher observes the marker

    await _poll_until(lambda: _record_is(id1, DeliveryStatus.SUPERSEDED))
    agent.release.set()
    await _settle(timeout=6.0)

    r1, r2, r3 = await _record(id1), await _record(id2), await _record(id3)
    assert r1.delivery_status is DeliveryStatus.SUPERSEDED
    assert r1.successor_id in (id2, id3)
    assert r2.answer_status == "answered"
    # The lead's whole text is the superseded text then the batch texts, in acceptance order.
    assert r2.answer == "echo: one\n\ntwo\n\nthree"
    assert r3.delivery_status is DeliveryStatus.MERGED
    assert r3.successor_id == id2


async def _record_is(message_id: str, status: DeliveryStatus) -> bool:
    record = await _record(message_id)
    return record is not None and record.delivery_status is status


# -- the settle window -------------------------------------------------------


async def test_settle_window_rides_a_burst_into_one_turn(env, monkeypatch):
    # deliver=all + settle: a turn starts after the window, gathering every message the burst
    # dropped in it, so one turn carries all three.
    tools = _wire_tool(monkeypatch, lambda kw: kw.get("message"))
    route = _tool_route(OverlapPolicy(deliver="all", settle_seconds=1), payload_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())

    id1 = await _accept("one", "P1")
    await asyncio.sleep(0.2)  # inside the lead's settle window
    id2 = await _accept("two", "P2")
    id3 = await _accept("three", "P3")
    await _settle(timeout=6.0)

    r1 = await _record(id1)
    assert r1.answer_status == "answered"
    # The one dispatched turn carried the whole burst as its ``messages`` and whole ``message``.
    captured = tools.calls[0]["arguments"]
    assert [m["text"] for m in captured["messages"]] == ["one", "two", "three"]
    assert captured["message"] == "one\n\ntwo\n\nthree"
    assert (await _record(id2)).delivery_status is DeliveryStatus.MERGED
    assert (await _record(id3)).delivery_status is DeliveryStatus.MERGED


async def test_settle_window_is_fixed_from_the_lead_and_keeps_the_intake_lease_live(env, monkeypatch):
    # A long settle window: the lead's intake lease keeps refreshing across it, so a re-drive
    # never reaps the settling turn.
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_REFRESH_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    _wire_tool(monkeypatch, lambda kw: "ok")
    route = _tool_route(OverlapPolicy(deliver="all", settle_seconds=3), payload_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())

    id1 = await _accept("one", "P1")
    # Past the original lease, while the turn is still inside its settle window.
    await asyncio.sleep(2.3)
    at_settle = (await _record(id1)).delivery_status
    assert at_settle is DeliveryStatus.ACCEPTED  # still settling, not reaped

    await turn_module.redrive_accepted()
    await asyncio.sleep(0)
    assert (await _record(id1)).delivery_status is DeliveryStatus.ACCEPTED
    await _settle(timeout=6.0)
    assert (await _record(id1)).answer_status == "answered"


# -- the marker: set only for cancel message routes --------------------------


async def test_the_cancel_marker_is_set_only_for_a_cancel_message_route(env, monkeypatch):
    agent = EchoAgent()
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    settings = ConversationsSettings()

    # A continue route sets no marker.
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy())), FakeChannel())
    await _accept("hi", "P1")
    await _settle()
    assert env._strings.get(settings.overlap_cancel_key(f"bridge:line:{_ADDR}")) is None

    # A cancel route sets it to the accepted message.
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(running="cancel"))), FakeChannel())
    mid = await _accept("hey", "P2")
    marker = env._strings.get(settings.overlap_cancel_key(f"bridge:line:{_ADDR}"))
    assert marker is not None
    import json

    assert json.loads(marker)["message_id"] == mid


async def test_the_watcher_ignores_a_marker_not_newer_than_the_batch(env, monkeypatch):
    # A cancel route with a single message: the marker names that same message, never newer
    # than the batch, so the turn runs to a normal answer.
    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(running="cancel"))), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    mid = await _accept("solo", "P1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    await asyncio.sleep(0.1)  # the watcher has polled the marker at least once
    assert (await _record(mid)).delivery_status is DeliveryStatus.ACCEPTED  # not cancelled
    agent.release.set()
    await _settle(timeout=6.0)
    assert (await _record(mid)).answer == "echo: solo"


# -- superseded is never delivered; a follower gone is skipped ----------------


async def test_a_superseded_channel_record_is_terminal_and_never_sent(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(running="cancel"))), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    id1 = await _accept("one", "P1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    id2 = await _accept("two", "P2")
    await _poll_until(lambda: _record_is(id1, DeliveryStatus.SUPERSEDED))
    agent.release.set()
    await _settle(timeout=6.0)

    r1 = await _record(id1)
    assert r1.delivery_status is DeliveryStatus.SUPERSEDED
    assert r1.answer_status is None
    assert r1.successor_id == id2
    # Only message 2's answer was ever sent; the superseded message 1 sent nothing.
    assert [n.message for n in channel.sends] == ["echo: two"]


async def test_a_follower_already_gone_is_skipped_by_the_merge(env, monkeypatch):
    # A follower whose record left ``accepted`` before the lead's gather is not merged and not
    # carried: the merge transition refuses it and it keeps its own outcome.
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    route = _tool_route(OverlapPolicy(deliver="all", settle_seconds=1), payload_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())

    await _accept("one", "P1")
    await asyncio.sleep(0.2)
    id2 = await _accept("two", "P2")
    # Move message 2 off ``accepted`` before the lead gathers it (a racing terminal).
    await _poll_until(lambda: _record_is(id2, DeliveryStatus.ACCEPTED))
    two = await _record(id2)
    await _store().complete_silent(
        ConversationRecord.model_validate(
            two.model_dump() | {"answer_status": None, "answer": None, "delivery_status": "silent"}
        )
    )
    await _settle(timeout=6.0)

    captured = tools.calls[0]["arguments"]
    assert [m["text"] for m in captured["messages"]] == ["one"]  # 2 was skipped, not merged
    assert (await _record(id2)).delivery_status is DeliveryStatus.SILENT  # its own outcome stands


# -- the event door is excluded by kind --------------------------------------


async def test_an_event_turn_is_never_merged_superseded_or_a_canceller(env, monkeypatch):
    # An event record on a cancel+all route runs as its own turn: excluded by kind, so no
    # batching, and it sets no cancel marker.
    from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission

    route = _tool_route(OverlapPolicy(running="cancel", deliver="all"), payload_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    settings = ConversationsSettings()
    thread_id = f"bridge:tool-line:{_ADDR}"

    # Seed the thread with a message turn (which sets the cancel marker), then clear the marker
    # so the event's non-effect is isolated.
    env.seed_route("tool-line")
    _wire_tool(monkeypatch, lambda kw: "seed")
    await _accept("hello", "SEED")
    await _settle()
    env._strings.pop(settings.overlap_cancel_key(thread_id), None)

    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    submission = ConversationEventSubmission(
        thread_id=thread_id, event=ConversationEvent(event_id="E1", kind="ping", payload={"x": 1})
    )
    result = await turn_module.submit_event("tool-line", submission, "svc-int")
    await _settle(timeout=6.0)

    record = await _record(result.message_id)
    assert record.inbound_kind == "event"
    assert record.delivery_status not in (DeliveryStatus.MERGED, DeliveryStatus.SUPERSEDED)
    # An event turn carries no ``messages`` key and set no cancel marker.
    assert "messages" not in tools.calls[-1]["arguments"]
    assert env._strings.get(settings.overlap_cancel_key(thread_id)) is None


class _BlockingTools:
    """A tool registry whose ``run_tool`` blocks until released — so a participant message can be
    accepted on the thread while a tool (here an event) turn is genuinely in flight."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run_tool(self, key: str, arguments: dict, *, offload_sync: bool = False) -> str:
        self.calls.append({"key": key, "arguments": arguments})
        self.entered.set()
        await self.release.wait()
        return "ok"


async def test_an_event_turn_on_a_cancel_route_arms_no_watcher_and_survives_a_newer_message(env, monkeypatch):
    # An event turn on a running="cancel" route must NOT arm the cancel watcher: an event turn is
    # never a canceller and is never superseded. A participant message accepted on the same thread
    # WHILE the event turn is in flight writes a strictly-newer cancel marker; an armed event
    # watcher would read it and supersede the event turn, delivering nothing. Under the shared
    # is_message_turn gate the event turn arms no watcher, completes answered, and the message
    # then runs its own turn.
    from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission

    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    route = _tool_route(OverlapPolicy(running="cancel", deliver="all"), payload_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    settings = ConversationsSettings()
    thread_id = f"bridge:tool-line:{_ADDR}"

    # Record the batch lead of every cancel_watch entry: the event turn arming no watcher is
    # proven by its id never appearing, while the later message turn legitimately arms its own.
    watched_leads: list[str] = []
    real_cancel_watch = overlap_module.cancel_watch

    @asynccontextmanager
    async def _recording_cancel_watch(route, batch):
        watched_leads.append(batch.lead.message_id)
        async with real_cancel_watch(route, batch):
            yield

    monkeypatch.setattr(overlap_module, "cancel_watch", _recording_cancel_watch)

    # Seed the thread with a quick message turn so the event has a thread to enter, then clear the
    # marker that seed wrote so only the later message's marker is in play.
    env.seed_route("tool-line")
    _wire_tool(monkeypatch, lambda kw: "seed")
    await _accept("hello", "SEED")
    await _settle()
    env._strings.pop(settings.overlap_cancel_key(thread_id), None)
    watched_leads.clear()

    tools = _BlockingTools()
    monkeypatch.setattr(accessors_module, "_tools", lambda: tools)

    submission = ConversationEventSubmission(
        thread_id=thread_id, event=ConversationEvent(event_id="E1", kind="ping", payload={"x": 1})
    )
    event_result = await turn_module.submit_event("tool-line", submission, "svc-int")
    await asyncio.wait_for(tools.entered.wait(), 2)  # the event turn is dispatched and in flight

    # A newer participant message: its accept writes a strictly-newer cancel marker while the event
    # turn is still running. An armed event watcher would read this marker and supersede the event.
    newer_id = await _accept("newer", "PNEW")
    marker = await overlap_module._read_cancel_marker(settings, settings.overlap_cancel_key(thread_id))
    assert marker is not None
    assert marker.message_id == newer_id
    await asyncio.sleep(0.1)  # several poll intervals: an armed watcher would have fired by now

    tools.release.set()
    await _settle(timeout=6.0)

    event_record = await _record(event_result.message_id)
    assert event_record.inbound_kind == "event"
    assert event_record.answer_status == "answered"
    assert event_record.delivery_status not in (DeliveryStatus.MERGED, DeliveryStatus.SUPERSEDED)
    # No watcher was ever armed for the event turn; the later message turn armed its own.
    assert event_result.message_id not in watched_leads
    assert newer_id in watched_leads
    # The message then ran its own turn.
    newer_record = await _record(newer_id)
    assert newer_record.answer_status == "answered"


# -- the API door delivers merged/superseded markers --------------------------


async def test_the_api_door_sync_wait_returns_a_superseded_marker_with_successor(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(running="cancel"), door="api", route_name="chat")))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    first = asyncio.create_task(turn_module.submit_api_message("chat", "user-7", "one", "alice", 5))
    await asyncio.wait_for(agent.entered.wait(), 2)
    second = asyncio.create_task(turn_module.submit_api_message("chat", "user-7", "two", "alice", 5))
    result1 = await asyncio.wait_for(first, 6)  # superseded once the watcher fires
    agent.release.set()  # let the second (surviving) turn finish
    result2 = await asyncio.wait_for(second, 6)
    await _settle(timeout=6.0)

    # The first turn was superseded by the second; its sync-wait carries the marker.
    assert result1.answer is not None
    assert result1.answer.status == "superseded"
    assert result1.answer.successor_id == result2.message_id
    r1 = await _record(result1.message_id)
    assert r1.delivery_status is DeliveryStatus.DELIVERED  # the marker was delivered
    assert r1.answer_status == "superseded"


async def test_the_api_door_posts_a_merged_marker_to_the_callback(env, monkeypatch):
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        import json

        posted.append(json.loads(body))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    route = _agent_route(OverlapPolicy(deliver="all", settle_seconds=1), door="api", route_name="chat")
    _wire(monkeypatch, FakeManager(route))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    lead = asyncio.create_task(turn_module.submit_api_message("chat", "user-7", "one", "alice", 0))
    await asyncio.sleep(0.2)
    follower = asyncio.create_task(turn_module.submit_api_message("chat", "user-7", "two", "alice", 0))
    r_lead = await asyncio.wait_for(lead, 6)
    r_follower = await asyncio.wait_for(follower, 6)
    await _settle(timeout=6.0)

    merged = await _record(r_follower.message_id)
    assert merged.answer_status == "merged"
    assert merged.successor_id == r_lead.message_id
    assert merged.delivery_status is DeliveryStatus.DELIVERED
    # The callback body for the follower carries the merged marker and its successor.
    bodies = {b["message_id"]: b for b in posted}
    assert bodies[r_follower.message_id]["status"] == "merged"
    assert bodies[r_follower.message_id]["successor_id"] == r_lead.message_id
    assert "answer" not in bodies[r_follower.message_id]


# -- manual mode appends the whole batch --------------------------------------


async def test_manual_mode_appends_every_message_of_the_batch(env, monkeypatch):
    from tai42_skeleton.conversations import mode as mode_module

    appended: list[list[dict]] = []

    class _Memo(EchoAgent):
        async def append_thread_messages(self, *, thread_id, messages, **kwargs):
            appended.append(list(messages))

    agent = _Memo()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(deliver="all", settle_seconds=1))), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    thread_id = f"bridge:line:{_ADDR}"
    await mode_module.ConversationModeStore(ConversationsSettings()).set_mode(thread_id, "manual")

    await _accept("one", "P1")
    await asyncio.sleep(0.2)
    await _accept("two", "P2")
    await _accept("three", "P3")
    await _settle(timeout=6.0)

    assert appended == [
        [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    ]


# -- the ambient turn ref + the yielded tool ----------------------------------


async def test_the_ambient_turn_ref_is_set_around_the_tool_branch(env, monkeypatch):
    seen: list[ConversationTurnRef | None] = []

    def _capture(kw):
        seen.append(current_conversation_turn())
        return "ok"

    _wire_tool(monkeypatch, _capture)
    _wire(monkeypatch, FakeManager(_tool_route(OverlapPolicy())), FakeChannel())

    mid = await _accept("hi", "P1")
    await _settle()
    # Deposited around the run with the turn's lead id, and reset once the turn is over.
    assert seen[0] == ConversationTurnRef(thread_id=f"bridge:tool-line:{_ADDR}", message_id=mid, route_name="tool-line")
    assert current_conversation_turn() is None


async def test_the_ambient_turn_ref_is_set_around_the_agent_branch(env, monkeypatch):
    seen: list[ConversationTurnRef | None] = []

    class _Peek(EchoAgent):
        async def run(self, *, user_message=None, thread_id=None, **kwargs):
            seen.append(current_conversation_turn())
            return "ok"

    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy())), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": _Peek()})

    mid = await _accept("hi", "P1")
    await _settle()
    assert seen[0] == ConversationTurnRef(thread_id=f"bridge:line:{_ADDR}", message_id=mid, route_name="line")
    assert current_conversation_turn() is None


async def test_a_tool_that_yields_resolves_the_turn_superseded(env, monkeypatch):
    def _yield(kw):
        raise TurnSupersededError("newer-1")

    _wire_tool(monkeypatch, _yield)
    _wire(monkeypatch, FakeManager(_tool_route(OverlapPolicy())), FakeChannel())

    mid = await _accept("hi", "P1")
    await _settle()
    record = await _record(mid)
    assert record.delivery_status is DeliveryStatus.SUPERSEDED
    assert record.successor_id == "newer-1"
    assert record.answer_status is None


# -- the payload shapes: whole text, batch, superseded ------------------------


def _msg_record(message_id: str, created_at: float, text: str, door: str = "channel") -> ConversationRecord:
    channel_route = _tool_route(OverlapPolicy(deliver="all"))
    route = channel_route if door == "channel" else _agent_route(OverlapPolicy(), door="api")
    record = record_module._new_record(
        route=route,
        message_id=message_id,
        thread_id="bridge:tool-line:x",
        client_address="x" if door == "api" else _ADDR,
        caller_principal="alice" if door == "api" else None,
        provider_message_id=None if door == "api" else f"PID-{message_id}",
        inbound_text=text,
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    return record.model_copy(update={"created_at": created_at, "updated_at": created_at})


def test_the_tool_payload_carries_messages_and_superseded_under_deliver_all():
    route = _tool_route(OverlapPolicy(deliver="all"), payload_expr=".")
    lead = _msg_record("m2", 2.0, "two")
    follower = _msg_record("m3", 3.0, "three")
    dropped = _msg_record("m1", 1.0, "one")
    batch = overlap_module.Batch(lead=lead, members=[lead, follower], superseded=[dropped])

    payload = tool_turn_module._tool_payload(
        route,
        "one\n\ntwo\n\nthree",
        _ADDR,
        "bridge:tool-line:x",
        record=lead,
        batch=batch,
        person=None,
        params=None,
        form=None,
        attachments=None,
        location=None,
    )
    assert payload["message"] == "one\n\ntwo\n\nthree"
    assert payload["messages"] == [
        {"id": "m2", "text": "two", "accepted_at": 2.0},
        {"id": "m3", "text": "three", "accepted_at": 3.0},
    ]
    assert payload["superseded"] == [{"id": "m1", "text": "one", "accepted_at": 1.0}]


def test_the_tool_payload_is_byte_identical_under_deliver_one():
    route = _tool_route(OverlapPolicy(), payload_expr=".")
    lead = _msg_record("m1", 1.0, "one")
    batch = overlap_module.Batch(lead=lead, members=[lead])

    payload = tool_turn_module._tool_payload(
        route,
        "one",
        _ADDR,
        "bridge:tool-line:x",
        record=lead,
        batch=batch,
        person=None,
        params=None,
        form=None,
        attachments=None,
        location=None,
    )
    assert "messages" not in payload
    assert "superseded" not in payload
    assert payload["message"] == "one"


# -- cross-worker: the watcher reads the shared marker ------------------------


async def test_a_marker_a_sibling_worker_set_cancels_the_running_turn(env, monkeypatch):
    # The watcher reads the marker from the shared record redis, so a marker another worker wrote
    # (here, written directly) cancels this worker's running turn — the cross-worker path.
    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    _wire(monkeypatch, FakeManager(_agent_route(OverlapPolicy(running="cancel"))), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    id1 = await _accept("one", "P1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    # A sibling worker accepts a newer message on the same thread and writes its marker.
    import json

    settings = ConversationsSettings()
    key = settings.overlap_cancel_key(f"bridge:line:{_ADDR}")
    env._strings[key] = json.dumps({"message_id": "sibling-newer", "created_at": time.time() + 10})

    await _poll_until(lambda: _record_is(id1, DeliveryStatus.SUPERSEDED))
    r1 = await _record(id1)
    assert r1.delivery_status is DeliveryStatus.SUPERSEDED
    assert r1.successor_id == "sibling-newer"


# -- _spawn_delivery_on_success skips channel overlap terminals ---------------


@pytest.mark.parametrize("status", [DeliveryStatus.MERGED, DeliveryStatus.SUPERSEDED])
async def test_a_channel_overlap_terminal_spawns_no_delivery(env, monkeypatch, status):
    spawned: list[str] = []
    monkeypatch.setattr("tai42_skeleton.conversations.turn.schedule.spawn_delivery", lambda mid: spawned.append(mid))
    from tai42_skeleton.conversations.turn import schedule as schedule_module

    record = _msg_record("m9", 1.0, "one")
    terminal = ConversationRecord.model_validate(
        record.model_dump() | {"delivery_status": status.value, "answer_status": None, "successor_id": "lead"}
    )

    async def _done() -> ConversationRecord:
        return terminal

    task = asyncio.create_task(_done())
    await task
    schedule_module._spawn_delivery_on_success(task, "m9")
    assert spawned == []  # a channel merged/superseded record is terminal; nothing is delivered
