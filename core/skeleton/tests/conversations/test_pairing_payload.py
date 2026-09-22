"""The pairing tool payload: the person id/addresses and thread key a linked person surfaces,
over the channel and api doors, plus the agent-turn drain outcomes."""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.agent.events import InterruptFinal, StructuredFinal, SuspendedFinal
from tai42_contract.conversations import ConversationRoute, ConversationTargetKind, TargetConversationConfig
from tai42_contract.template import TemplatedText

from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations import cache as cache_module
from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import pair_codes as pair_codes_module
from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import redeem_throttle as throttle_module
from tai42_skeleton.conversations import target_config as target_config_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.persons import ConversationPersonStore, PairingTarget
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import agent_turn as agent_turn_module
from tai42_skeleton.conversations.turn import keys as keys_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import pairing as pairing_module
from tai42_skeleton.conversations.turn import schedule as schedule_module
from tai42_skeleton.conversations.turn import tool_turn as tool_turn_module

from .conftest import rendered_user_message
from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_CODE_RE = re.compile(r"LINK-[A-Z0-9]{8}")


class _EchoInput(BaseModel):
    user_message: str = ""


class EchoAgent(Agent):
    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
        text = rendered_user_message(user_message)
        self.calls.append((text, thread_id))
        return f"echo: {text}"


class FakeManager:
    def __init__(self, *routes: ConversationRoute) -> None:
        self._routes = {r.route_name: r for r in routes}

    async def list_routes(self):
        return dict(self._routes)

    async def get_route(self, name: str):
        return self._routes.get(name)


class FakeChannel:
    def __init__(self) -> None:
        self.sends: list = []

    async def notify(self, notification):
        self.sends.append(notification)
        return [f"out-{len(self.sends)}"]


class _FakeChannels:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    def get(self, name: str) -> FakeChannel:
        return self._channel


class _FakeApp:
    def __init__(self, channel: FakeChannel) -> None:
        self.channels = _FakeChannels(channel)


class _FakeTemplateResourceManager:
    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        assert text.content is not None
        return text.content


class _FakeTemplateStorage:
    def __init__(self) -> None:
        self.resource_manager = _FakeTemplateResourceManager()


class _FakeTemplateApp:
    def __init__(self) -> None:
        self.storage = _FakeTemplateStorage()


def _channel_route(route_name: str = "line-a", channel: str = "twilio", our_identity: str = "+15550001111"):
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc",
        channel=channel,
        our_identity=our_identity,
        execution_key_fingerprint="fp-1",
    )


def _api_route(route_name: str = "chat"):
    return ConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        execution_key_fingerprint="fp-1",
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    fake = FakeRecordRedis()
    ctx = make_record_client_ctx(fake)
    for module in (
        records_module,
        ledger_module,
        mode_module,
        persons_module,
        pair_codes_module,
        target_config_module,
        throttle_module,
        thread_lease_module,
    ):
        monkeypatch.setattr(module, "client_ctx", ctx)

    @asynccontextmanager
    async def _bind(execution_key, *, bound_fingerprint):
        yield CallerIdentity(user_id=execution_key)

    monkeypatch.setattr(agent_turn_module, "bind_execution_identity", _bind)
    monkeypatch.setattr(tool_turn_module, "bind_execution_identity", _bind)

    async def _allow(identity, agent_name, **kwargs):
        return None

    monkeypatch.setattr(agent_turn_module, "authorize_execution_agent_run", _allow)
    return fake


def _wire(monkeypatch, manager: FakeManager, channel: FakeChannel | None = None, agent: Agent | None = None) -> None:
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(tool_turn_module, "tai42_app", _FakeTemplateApp())
    if channel is not None:
        monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"assistant": agent or EchoAgent()})


def _seed_config(
    fake: FakeRecordRedis,
    *,
    target_kind: ConversationTargetKind = "agent",
    target_name: str = "assistant",
    multichannel: bool = True,
    greeting_template: str | None = None,
) -> None:
    config = TargetConversationConfig(
        target_kind=target_kind, target_name=target_name, multichannel=multichannel, greeting_template=greeting_template
    )
    fake._strings[ConversationsSettings().target_config_key(target_kind, target_name)] = config.model_dump_json()


def _tool_route(
    route_name: str = "tool-line",
    our_identity: str = "+15550001111",
    *,
    payload_expr: str | None = None,
):
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="tool",
        target_name="pinger",
        payload_expr=TemplatedText(content=payload_expr) if payload_expr is not None else None,
        execution_key="svc",
        channel="twilio",
        our_identity=our_identity,
        execution_key_fingerprint="fp-1",
    )


class _FakeTools:
    def __init__(self, fn) -> None:
        self.fn = fn

    async def run_tool(self, key: str, arguments: dict, *, offload_sync: bool = False):
        return self.fn(arguments)


def _wire_tool(monkeypatch, fn) -> None:
    monkeypatch.setattr(accessors_module, "_tools", lambda: _FakeTools(fn))


def _accepting_callback():
    async def _post(url, body, signature, timeout_seconds):
        return 200

    return _post


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _person_store() -> ConversationPersonStore:
    return ConversationPersonStore(ConversationsSettings())


_TARGET = PairingTarget(target_kind="agent", target_name="assistant")


_TOOL_TARGET = PairingTarget(target_kind="tool", target_name="pinger")


async def _settle(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = [t for t in (*schedule_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            # Recompute after the yield: a task can appear between the two checks, so wait on
            # the fresh list (asyncio.wait raises on an empty set), and return only when it
            # is still empty.
            tasks = [t for t in (*schedule_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
            if not tasks:
                return
        await asyncio.wait(tasks, timeout=0.05)


async def _answer_of(message_id: str) -> str:
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer is not None
    return record.answer


async def _mint_via_link(monkeypatch, fake, route, *, address="+15550002222", provider="PID-L") -> str:
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route), channel)
    mid = await turn_module.accept("twilio", route.our_identity, address, address, "/link", provider)
    await _settle()
    answer = await _answer_of(mid)
    match = _CODE_RE.search(answer)
    assert match is not None, answer
    return match.group(0)


async def test_tool_payload_off_carries_no_person_keys(env, monkeypatch):
    # Multichannel OFF (no config row): the payload the tool sees carries the base keys plus
    # the generic turn block — no person_id / person_addresses. ``payload_expr="."`` echoes
    # the whole payload.
    seen: list[dict] = []
    _wire(monkeypatch, FakeManager(_tool_route(payload_expr=".")), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: seen.append(kw) or "pong")
    await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    assert len(seen) == 1
    assert set(seen[0]) == {"message", "sender", "our_identity", "channel", "thread_id", "turn"}


async def test_tool_payload_on_carries_a_stable_person_id(env, monkeypatch):
    # Multichannel ON: person_id is present from the FIRST message (the provisional person)
    # and identical on a second message from the same address; sender stays the address.
    _seed_config(env, target_kind="tool", target_name="pinger")
    seen: list[dict] = []
    _wire(monkeypatch, FakeManager(_tool_route(payload_expr=".")), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: seen.append(kw) or "pong")
    await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi again", "PID-2")
    await _settle()
    person = await _person_store().get_person(
        _TOOL_TARGET, door="channel", channel="twilio", our_identity="+15550001111", address="+2000"
    )
    assert person is not None
    assert len(seen) == 2
    assert seen[0]["person_id"] == person.person_id
    assert seen[1]["person_id"] == person.person_id
    assert seen[0]["sender"] == "+2000"
    assert seen[0]["person_addresses"] == [a.model_dump(mode="json") for a in person.addresses]


async def test_payload_expr_can_read_person_id_and_addresses(env, monkeypatch):
    _seed_config(env, target_kind="tool", target_name="pinger")
    seen: list[dict] = []
    route = _tool_route(payload_expr="{who: .person_id, addrs: .person_addresses}")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: seen.append(kw) or "pong")
    await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    person = await _person_store().get_person(
        _TOOL_TARGET, door="channel", channel="twilio", our_identity="+15550001111", address="+2000"
    )
    assert person is not None
    assert seen == [{"who": person.person_id, "addrs": [a.model_dump(mode="json") for a in person.addresses]}]


async def test_tool_route_thread_keys_route_then_person_when_linked(env, monkeypatch):
    # A single-address person keys the ROUTE thread; a linked (>1 address) person keys the
    # aggregated person thread — for a TOOL target exactly as for an agent one.
    _seed_config(env, target_kind="tool", target_name="pinger")
    route_a = _tool_route(route_name="tool-a", our_identity="+15550001111")
    route_b = _tool_route(route_name="tool-b", our_identity="+15550009999")
    _wire(monkeypatch, FakeManager(route_a, route_b), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: "pong")

    # First contact from a single-address person → the route thread.
    solo_mid = await turn_module.accept("twilio", "+15550001111", "+1000", "+1000", "hi", "PID-solo")
    await _settle()
    solo_record = await _store().get_record(solo_mid)
    assert solo_record is not None
    assert solo_record.thread_id == "bridge:tool-a:+1000"

    # addrA mints a link code (the pairing turn intercepts before any dispatch).
    link_mid = await turn_module.accept("twilio", "+15550001111", "+1000", "+1000", "/link", "PID-link")
    await _settle()
    match = _CODE_RE.search(await _answer_of(link_mid))
    assert match is not None
    code = match.group(0)

    # addrB redeems on route B → the two addresses merge into one person.
    await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-redeem")
    await _settle()
    person = await _person_store().get_person(
        _TOOL_TARGET, door="channel", channel="twilio", our_identity="+15550009999", address="+2000"
    )
    assert person is not None
    assert {a.address for a in person.addresses} == {"+1000", "+2000"}

    # The NEXT plain message from addrB keys the aggregated person thread.
    next_mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", "hi again", "PID-next")
    await _settle()
    next_record = await _store().get_record(next_mid)
    assert next_record is not None
    assert next_record.thread_id == f"bridge:@person:{person.person_id}"


async def test_tool_payload_on_the_api_door_carries_the_person_and_composed_address(env, monkeypatch):
    # The api door reaches the tool payload with door="api", channel/our_identity None, and a
    # composed client_address (percent-encoded principal / end-user id) as the sender.
    _seed_config(env, target_kind="tool", target_name="pinger")
    api_tool_route = ConversationRoute(
        route_name="tool-api",
        door="api",
        target_kind="tool",
        target_name="pinger",
        payload_expr=TemplatedText(content="."),
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        execution_key_fingerprint="fp-1",
    )
    seen: list[dict] = []
    _wire(monkeypatch, FakeManager(api_tool_route))
    _wire_tool(monkeypatch, lambda kw: seen.append(kw) or "pong")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    client_address = keys_module._api_client_address("alice", "u7")
    await turn_module.submit_api_message("tool-api", "u7", "hi", "alice", 5)
    await _settle()

    person = await _person_store().get_person(
        _TOOL_TARGET, door="api", channel=None, our_identity=None, address=client_address
    )
    assert person is not None
    assert len(seen) == 1
    assert seen[0]["person_id"] == person.person_id
    assert seen[0]["person_id"]
    assert seen[0]["person_addresses"] == [a.model_dump(mode="json") for a in person.addresses]
    assert len(seen[0]["person_addresses"]) == 1
    assert seen[0]["person_addresses"][0]["door"] == "api"
    assert seen[0]["person_addresses"][0]["address"] == client_address
    assert seen[0]["sender"] == client_address
    assert seen[0]["channel"] is None
    assert seen[0]["our_identity"] is None


async def test_api_door_join_discovers_the_person_thread_key(env, monkeypatch):
    _seed_config(env)
    channel_route = _channel_route(route_name="line-a", our_identity="+15550001111")
    api_route = _api_route("chat")
    code = await _mint_via_link(monkeypatch, env, channel_route, address="+1000", provider="PID-A")

    _wire(monkeypatch, FakeManager(channel_route, api_route))
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    # The redeem submit's OWN response carries the PRE-merge route key (fixed at accept).
    redeem = await turn_module.submit_api_message("chat", "u7", code, "alice", 5)
    await _settle()
    assert redeem.thread_id == "bridge:chat:alice/u7"
    assert redeem.answer is not None
    assert "linked" in (redeem.answer.answer or "").lower()

    person = await _person_store().get_person(_TARGET, door="api", channel=None, our_identity=None, address="alice/u7")
    assert person is not None
    assert {a.address for a in person.addresses} == {"+1000", "alice/u7"}
    assert person.addresses[0].door in {"api", "channel"}

    # The NEXT submit RETURNS the person thread key — the discovery vehicle at the door.
    nxt = await turn_module.submit_api_message("chat", "u7", "hi", "alice", 5)
    await _settle()
    assert nxt.thread_id == f"bridge:@person:{person.person_id}"


async def test_api_door_concurrent_double_redeem_has_exactly_one_winner(env, monkeypatch):
    _seed_config(env)
    channel_route = _channel_route(route_name="line-a", our_identity="+15550001111")
    api_route = _api_route("chat")
    code = await _mint_via_link(monkeypatch, env, channel_route, address="+1000", provider="PID-A")
    _wire(monkeypatch, FakeManager(channel_route, api_route))
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    # Two callers redeem the SAME code at once: the atomic GETDEL admits exactly one winner
    # (the api door has no claim_inbound dedupe of its own).
    first, second = await asyncio.gather(
        turn_module.submit_api_message("chat", "u-a", code, "alice", 5),
        turn_module.submit_api_message("chat", "u-b", code, "bob", 5),
    )
    await _settle()
    answers = sorted(
        (r.answer.answer or "") for r in (first, second) if r.answer is not None and r.answer.answer is not None
    )
    linked = [a for a in answers if "linked" in a.lower()]
    invalid = [a for a in answers if a == pairing_module._INVALID_CODE_TEXT]
    assert len(linked) == 1
    assert len(invalid) == 1


class _StreamAgent(Agent):
    """An agent whose ``astream`` yields exactly the events a test hands it — so the drain's
    structured/interrupt/empty branches are exercised directly."""

    tool_name = "stream"
    ToolInput = _EchoInput

    def __init__(self, *events) -> None:
        self._events = events

    async def run(
        self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs
    ):  # pragma: no cover
        raise NotImplementedError  # astream is overridden, so run is never reached

    async def astream(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
        for event in self._events:
            yield event


def _resolved(outcome):
    """Assert a resolved (answered/error) outcome and return ``(answer_status, answer,
    error)`` — the shape the drain tests read."""
    assert isinstance(outcome, outcome_module._ResolvedOutcome), outcome
    return outcome.answer_status, outcome.answer, outcome.error


async def test_agent_drain_serializes_structured_finals(env, monkeypatch):
    route = _api_route()
    cases = {
        "raw string": StructuredFinal(data="raw string"),  # str passes through
        '{"ok": true}': StructuredFinal(data={"ok": True}),  # json.dumps default
        '{"user_message":"m"}': StructuredFinal(data=_EchoInput(user_message="m")),  # model_dump_json
    }
    for expected, event in cases.items():
        monkeypatch.setattr(accessors_module, "_agent_registry", lambda event=event: {"assistant": _StreamAgent(event)})
        status, answer, error = _resolved(await agent_turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
        assert status == "answered"
        assert answer == expected
        assert error is None


async def test_agent_drain_raises_on_an_interrupt_becoming_an_error_outcome(env, monkeypatch):
    route = _api_route()
    agent = _StreamAgent(InterruptFinal(interrupt_id="i1", payload={"q": "?"}, reason="needs input"))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"assistant": agent})
    status, answer, error = _resolved(await agent_turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
    assert status == "error"
    assert answer == outcome_module._ERROR_ANSWER_TEXT
    assert error is not None
    assert "interrupt" in error


async def test_agent_drain_async_park_is_a_silent_outcome(env, monkeypatch):
    # The conversation door binds a completion tool around the turn, so an async ask
    # PARKS: the turn produces no reply now (a silent outcome) and the resumed answer is
    # delivered out of band by the completion continuation.
    route = _api_route()
    agent = _StreamAgent(SuspendedFinal(interaction_ids=["i1"], thread_id="t"))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"assistant": agent})
    outcome = await agent_turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client")
    assert isinstance(outcome, outcome_module._SilentOutcome)


async def test_agent_drain_empty_stream_is_an_empty_answer_error(env, monkeypatch):
    route = _api_route()
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"assistant": _StreamAgent()})  # no events
    status, _answer, error = _resolved(await agent_turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
    assert status == "error"
    assert error == "agent produced an empty answer"


async def test_agent_turn_for_an_unregistered_agent_is_an_error(env, monkeypatch):
    route = _api_route()
    monkeypatch.setattr(accessors_module, "_agent_registry", dict)
    status, _answer, error = _resolved(await agent_turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
    assert status == "error"
    assert error is not None
    assert "not registered" in error
