"""The pairing turn kind: intercept classification, link/redeem/unlink INSIDE the accept
lifecycle, the first-contact greeting, the redeem brute-force throttle, and the pin that
a multichannel-off target is byte-identical to today.

Run e2e-in-unit over the faked redis stores (records + persons + pair codes + config +
throttle all share one ``FakeRecordRedis``), with the execution-identity seam stubbed."""

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
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.authz.identity import CallerIdentity
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
from tai42_skeleton.conversations.pair_codes import ConversationPairCodeStore, MintingConversation
from tai42_skeleton.conversations.persons import ConversationPersonStore, PairingTarget
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_CODE_RE = re.compile(r"LINK-[A-Z0-9]{8}")


class _EchoInput(BaseModel):
    user_message: str = ""


class EchoAgent(Agent):
    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        self.calls.append((user_message, thread_id))
        return f"echo: {user_message}"


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

    monkeypatch.setattr(turn_module, "bind_execution_identity", _bind)

    async def _allow(identity, agent_name, **kwargs):
        return None

    monkeypatch.setattr(turn_module, "authorize_execution_agent_run", _allow)
    return fake


def _wire(monkeypatch, manager: FakeManager, channel: FakeChannel | None = None, agent: Agent | None = None) -> None:
    monkeypatch.setattr(turn_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    if channel is not None:
        monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"assistant": agent or EchoAgent()})


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
        payload_expr=payload_expr,
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
    monkeypatch.setattr(turn_module, "_tools", lambda: _FakeTools(fn))


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
        tasks = [t for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            # Recompute after the yield: a task can appear between the two checks, so wait on
            # the fresh list (asyncio.wait raises on an empty set), and return only when it
            # is still empty.
            tasks = [t for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
            if not tasks:
                return
        await asyncio.wait(tasks, timeout=0.05)


async def _answer_of(message_id: str) -> str:
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer is not None
    return record.answer


# -- C1: the pure classifier -------------------------------------------------


def test_classify_is_exact_match_and_first_code_wins():
    from tai42_skeleton.conversations.pairing import Link, Passthrough, Redeem, Unlink, classify

    assert isinstance(classify("/link"), Link)
    assert isinstance(classify("  /link  "), Link)  # whole trimmed message
    assert isinstance(classify("/unlink"), Unlink)
    # Commands are whole-message: extra words make them plain text.
    assert isinstance(classify("/link please"), Passthrough)
    assert isinstance(classify("/unlink now"), Passthrough)
    # A code anywhere in the text redeems; the FIRST match wins.
    embedded = classify("here you go: LINK-ABCD1234 thanks")
    assert isinstance(embedded, Redeem)
    assert embedded.code == "LINK-ABCD1234"
    two = classify("LINK-AAAAAAAA or LINK-BBBBBBBB")
    assert isinstance(two, Redeem)
    assert two.code == "LINK-AAAAAAAA"
    # A malformed / too-short code is not a code.
    assert isinstance(classify("LINK-abc"), Passthrough)
    assert isinstance(classify("just a normal message"), Passthrough)


def test_classify_form_rendered_text_is_passthrough():
    # An ask-less form submission's rendered text is label:value lines. The command
    # grammar matches ONLY the whole trimmed message, so a label:value first line —
    # even one starting with a command literal — can never classify as a command.
    from tai42_skeleton.conversations.pairing import Passthrough, classify

    assert isinstance(classify("name: Alice\nsize: 42"), Passthrough)
    assert isinstance(classify("/link: yes\nname: Alice"), Passthrough)
    assert isinstance(classify("/unlink: true"), Passthrough)


# -- C2: link / redeem / unlink e2e ------------------------------------------


async def _mint_via_link(monkeypatch, fake, route, *, address="+15550002222", provider="PID-L") -> str:
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route), channel)
    mid = await turn_module.accept("twilio", route.our_identity, address, address, "/link", provider)
    await _settle()
    answer = await _answer_of(mid)
    match = _CODE_RE.search(answer)
    assert match is not None, answer
    return match.group(0)


async def test_link_mints_a_code_reply(env, monkeypatch):
    _seed_config(env)
    route = _channel_route()
    code = await _mint_via_link(monkeypatch, env, route)
    assert code.startswith("LINK-")
    # A provisional person was created for the sender, and NO agent turn ran for /link.
    person = await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550001111", address="+15550002222"
    )
    assert person is not None


async def test_redeem_across_two_routes_merges_and_switches_the_person_thread(env, monkeypatch):
    _seed_config(env)
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")

    # addrA mints on route A.
    code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")

    # addrB redeems on route B — the redeem turn itself keys the PRE-merge route thread.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)
    redeem_mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-B")
    await _settle()
    redeem_record = await _store().get_record(redeem_mid)
    assert redeem_record is not None
    assert redeem_record.thread_id == "bridge:line-b:+2000"  # not before: fixed at accept
    assert "linked" in (redeem_record.answer or "").lower()

    # The two addresses now belong to one person.
    person = await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550009999", address="+2000"
    )
    assert person is not None
    assert {a.address for a in person.addresses} == {"+1000", "+2000"}

    # The NEXT message from addrB keys the aggregated person thread.
    next_mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", "hi again", "PID-B2")
    await _settle()
    next_record = await _store().get_record(next_mid)
    assert next_record is not None
    assert next_record.thread_id == f"bridge:@person:{person.person_id}"


async def test_unlink_detaches_and_returns_to_the_route_thread(env, monkeypatch):
    _seed_config(env)
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)
    await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-B")
    await _settle()

    # /unlink from addrB detaches it back to its own provisional person.
    unlink_mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", "/unlink", "PID-U")
    await _settle()
    assert "no longer linked" in (await _answer_of(unlink_mid)).lower()

    solo = await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550009999", address="+2000"
    )
    assert solo is not None
    assert [a.address for a in solo.addresses] == ["+2000"]

    # Its next message is back on the route thread (and not re-greeted — created=False).
    next_mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", "hi", "PID-U2")
    await _settle()
    next_record = await _store().get_record(next_mid)
    assert next_record is not None
    assert next_record.thread_id == "bridge:line-b:+2000"


async def test_unlink_on_a_never_linked_address_is_a_loud_answered_reply(env, monkeypatch):
    _seed_config(env)
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "/unlink", "PID-U")
    await _settle()
    record = await _store().get_record(mid)
    assert record is not None
    assert record.answer_status == "answered"  # distinguishable from the error outcome
    assert "nothing to unlink" in (record.answer or "").lower()


# -- error scoping -----------------------------------------------------------


async def test_invalid_code_is_an_answered_uniform_reply(env, monkeypatch):
    _seed_config(env)
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "LINK-ZZZZZZZZ", "PID-X")
    await _settle()
    record = await _store().get_record(mid)
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == turn_module._INVALID_CODE_TEXT


async def test_cross_target_redeem_is_an_answered_refusal(env, monkeypatch):
    _seed_config(env)
    # A code minted on a DIFFERENT target: redeeming it here merges across targets and refuses.
    settings = ConversationsSettings()
    other = TargetConversationConfig(target_kind="tool", target_name="pinger", multichannel=True)
    env._strings[settings.target_config_key("tool", "pinger")] = other.model_dump_json()
    tool_route = ConversationRoute(
        route_name="tool-line",
        door="channel",
        target_kind="tool",
        target_name="pinger",
        execution_key="svc",
        channel="twilio",
        our_identity="+15557770000",
        execution_key_fingerprint="fp-1",
    )
    agent_route = _channel_route(route_name="line-a", our_identity="+15550001111")

    # Mint on the tool target.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(tool_route, agent_route), channel)
    tool_mint = await turn_module.accept("twilio", "+15557770000", "+3000", "+3000", "/link", "PID-T")
    await _settle()
    tool_mint_match = _CODE_RE.search(await _answer_of(tool_mint))
    assert tool_mint_match is not None
    code = tool_mint_match.group(0)

    # Redeem it on the AGENT target — cross-target merge is refused, answered.
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", code, "PID-C")
    await _settle()
    record = await _store().get_record(mid)
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == turn_module._INVALID_CODE_TEXT


async def test_a_store_fault_mid_redeem_is_a_loud_error_outcome_not_invalid_code(env, monkeypatch):
    _seed_config(env)
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)

    # The merge (an infra round-trip) faults: NOT a pairing domain error, so it must take the
    # standard client-safe ERROR outcome, never masquerade as an invalid code.
    async def _boom(self, a, b):
        raise RuntimeError("redis reset mid-merge")

    monkeypatch.setattr(persons_module.ConversationPersonStore, "merge", _boom)
    mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-B")
    await _settle()
    record = await _store().get_record(mid)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == turn_module._ERROR_ANSWER_TEXT
    assert record.error is not None
    assert "redis reset" in record.error


async def test_a_store_fault_mid_redeem_uses_the_route_error_reply_text_when_set(env, monkeypatch):
    # The pairing infra-fault path (turn.py:911) resolves the guest-facing reply through the
    # route: a route carrying ``error_reply_text`` sends THAT custom notice, while the record's
    # internal ``error`` detail keeps the diagnosable wording.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    _seed_config(env)
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999").model_copy(
        update={"error_reply_text": spanish}
    )
    code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)

    async def _boom(self, a, b):
        raise RuntimeError("redis reset mid-merge")

    monkeypatch.setattr(persons_module.ConversationPersonStore, "merge", _boom)
    mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-B")
    await _settle()
    record = await _store().get_record(mid)
    assert record is not None
    assert record.answer_status == "error"
    # The guest sees the route's custom reply; the internal detail is untouched.
    assert record.answer == spanish
    assert record.error is not None
    assert "redis reset" in record.error


# -- greeting ----------------------------------------------------------------


async def test_first_contact_greeting_prepends_to_the_answer_once(env, monkeypatch):
    _seed_config(env, greeting_template="Welcome!")
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel, agent=agent)

    first = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    assert (await _answer_of(first)) == "Welcome!\n\necho: hi"

    # The SECOND message from the same address is past first contact — no greeting.
    second = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "again", "PID-2")
    await _settle()
    assert (await _answer_of(second)) == "echo: again"


async def test_greeting_with_no_template_and_no_config_does_not_fire(env, monkeypatch):
    _seed_config(env, greeting_template=None)
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel(), agent=agent)
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    assert (await _answer_of(mid)) == "echo: hi"


async def test_greeting_with_placeholder_mints_a_live_code(env, monkeypatch):
    _seed_config(env, greeting_template="Hi! Pair with {pairing_code}")
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel(), agent=agent)
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    answer = await _answer_of(mid)
    match = _CODE_RE.search(answer)
    assert match is not None
    # The minted code is live: it redeems from another conversation.
    assert answer.startswith("Hi! Pair with LINK-")


async def test_first_contact_redeem_greets_and_links_in_one_reply(env, monkeypatch):
    # A first admitted message that is itself a redeem greets AND links.
    _seed_config(env, greeting_template="Welcome!")
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)

    # addrB's FIRST message is the redeem.
    mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-B")
    await _settle()
    answer = await _answer_of(mid)
    assert answer.startswith("Welcome!\n\n")
    assert "linked" in answer.lower()


async def test_a_redeem_of_a_tool_minted_code_ensures_the_minting_side(env, monkeypatch):
    # A code whose minting address has no admitted inbound yet: the redeem side still honors
    # it, creating the minting provisional row, and the merge succeeds.
    _seed_config(env)
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    _wire(monkeypatch, FakeManager(route_b), FakeChannel())

    # Mint a code directly for an address that never sent an inbound (as the tool would).
    code, _expires = await ConversationPairCodeStore(ConversationsSettings()).mint(
        MintingConversation(
            target_kind="agent",
            target_name="assistant",
            route_name="line-a",
            door="channel",
            channel="twilio",
            our_identity="+15550001111",
            address="+1000",
        )
    )
    mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-B")
    await _settle()
    assert "linked" in (await _answer_of(mid)).lower()
    person = await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550009999", address="+2000"
    )
    assert person is not None
    assert {a.address for a in person.addresses} == {"+1000", "+2000"}


# -- the redeem brute-force throttle -----------------------------------------


async def test_a_locked_source_does_not_burn_a_valid_code_and_leaks_no_oracle(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_THRESHOLD", "1")
    _seed_config(env)
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    good_code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)

    # Two invalid redeems from addrB arm the backoff lock (threshold 1).
    replies = []
    for index in range(2):
        mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", "LINK-QQQQQQQQ", f"PID-I{index}")
        await _settle()
        replies.append(await _answer_of(mid))

    # Now redeem the GENUINELY VALID code while locked: refused with the SAME uniform reply...
    locked_mid = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", good_code, "PID-G")
    await _settle()
    locked_reply = await _answer_of(locked_mid)
    assert locked_reply == turn_module._INVALID_CODE_TEXT
    assert set(replies) == {turn_module._INVALID_CODE_TEXT}  # no oracle: every reply identical

    # ...and the valid code was NOT consumed — the lock refused before the code store.
    assert ConversationsSettings().pair_code_key(hash_api_key(good_code)) in env._strings


async def test_api_redeem_throttle_keys_on_the_caller_not_the_rotatable_external_user_id(env, monkeypatch):
    # Bypass regression: the api door's redeem throttle SOURCE is the authenticated
    # ``caller_principal``, NOT the caller-composed ``client_address`` (which embeds the FREE
    # ``external_user_id``). An attacker with ONE principal who supplies a FRESH
    # external_user_id per attempt must NOT get a fresh throttle bucket each time — the lock
    # must arm on the accountable caller. Pre-fix the source keyed on the composed address, so
    # every rotated external_user_id was its own bucket and the lock NEVER armed: this test
    # fails against the pre-fix code (the valid redeem below would succeed and consume the
    # code).
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_THRESHOLD", "2")
    _seed_config(env)
    channel_route = _channel_route(route_name="line-a", our_identity="+15550001111")
    api_route = _api_route("chat")
    good_code = await _mint_via_link(monkeypatch, env, channel_route, address="+1000", provider="PID-A")
    _wire(monkeypatch, FakeManager(channel_route, api_route))
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    # Three invalid redeems from ONE caller "mallory", each under a DIFFERENT external_user_id
    # (one past the threshold of 2 → the lock arms on the caller, if the source is the caller).
    for index in range(3):
        res = await turn_module.submit_api_message("chat", f"victim-{index}", "LINK-QQQQQQQQ", "mallory", 5)
        await _settle()
        assert res.answer is not None
        assert res.answer.answer == turn_module._INVALID_CODE_TEXT

    # The lock is now ARMED on the caller: a GENUINELY VALID redeem from mallory (under YET
    # another external_user_id) is refused with the SAME uniform reply and does NOT consume
    # the live code — proving external_user_id rotation no longer evades the throttle.
    locked = await turn_module.submit_api_message("chat", "victim-final", good_code, "mallory", 5)
    await _settle()
    assert locked.answer is not None
    assert locked.answer.answer == turn_module._INVALID_CODE_TEXT
    assert ConversationsSettings().pair_code_key(hash_api_key(good_code)) in env._strings


async def test_first_contact_link_greeting_and_reply_share_one_live_code(env, monkeypatch):
    # A first-contact /link with a {pairing_code} greeting must mint EXACTLY ONE code: the
    # greeting and the link reply present the SAME code, and it stays LIVE (redeemable). Pre-fix
    # the greeting minted code A and the Link branch minted code B, rotation deleted A, and
    # the greeting showed a now-dead code — so this test fails against the pre-fix code (two
    # distinct codes appear in the one reply).
    _seed_config(env, greeting_template="Welcome! Pair with {pairing_code}")
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    _wire(monkeypatch, FakeManager(route_a, route_b), FakeChannel())

    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "/link", "PID-1")
    await _settle()
    answer = await _answer_of(mid)

    # Exactly ONE distinct code across the whole reply, present in BOTH the greeting and the
    # link line (greeting is prefixed, then "\n\n", then the link reply).
    codes = set(_CODE_RE.findall(answer))
    assert len(codes) == 1
    (code,) = codes
    greeting_line, link_line = answer.split("\n\n", 1)
    assert greeting_line == f"Welcome! Pair with {code}"
    assert code in link_line

    # The single shown code is LIVE: redeeming it from another conversation links the two.
    redeem = await turn_module.accept("twilio", "+15550009999", "+3000", "+3000", code, "PID-2")
    await _settle()
    assert "linked" in (await _answer_of(redeem)).lower()


# -- multichannel OFF is byte-identical --------------------------------------


@pytest.mark.parametrize("text", ["/link", "/unlink", "LINK-ABCD1234"])
async def test_multichannel_off_passes_pairing_syntax_through_as_plain_text(env, monkeypatch, text):
    # No config row → multichannel OFF (default-false): every pairing form reaches the
    # target agent verbatim and NO person row is created.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel(), agent=agent)
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", text, "PID-1")
    await _settle()
    assert agent.calls == [(text, "bridge:line-a:+2000")]
    assert (await _answer_of(mid)) == f"echo: {text}"
    person = await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550001111", address="+2000"
    )
    assert person is None


async def test_config_present_but_multichannel_false_is_also_off(env, monkeypatch):
    _seed_config(env, multichannel=False, greeting_template="Welcome!")
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel(), agent=agent)
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "/link", "PID-1")
    await _settle()
    # No greeting, no code, plain passthrough.
    assert (await _answer_of(mid)) == "echo: /link"


async def test_redelivery_of_a_redeem_does_not_double_redeem(env, monkeypatch):
    _seed_config(env)
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)

    first = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-DUP")
    await _settle()
    # A provider redelivery (same provider_message_id) returns the prior id, runs no 2nd turn.
    second = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-DUP")
    await _settle()
    assert first == second
    person = await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550009999", address="+2000"
    )
    assert person is not None
    assert {a.address for a in person.addresses} == {"+1000", "+2000"}


# -- the greeting predicate at the turn boundary -----------------------------


async def test_a_detached_address_is_not_re_greeted_on_its_next_message(env, monkeypatch):
    # A detached address gets a fresh provisional row at detach time, so its NEXT inbound is
    # not a row creation and is not greeted — created=False at the TURN level, not just store.
    _seed_config(env, greeting_template="Welcome!")
    route_a = _channel_route(route_name="line-a", our_identity="+15550001111")
    route_b = _channel_route(route_name="line-b", our_identity="+15550009999")
    code = await _mint_via_link(monkeypatch, env, route_a, address="+1000", provider="PID-A")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)
    await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", code, "PID-B")
    await _settle()
    await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", "/unlink", "PID-U")
    await _settle()

    nxt = await turn_module.accept("twilio", "+15550009999", "+2000", "+2000", "hi", "PID-N")
    await _settle()
    assert (await _answer_of(nxt)) == "echo: hi"  # no "Welcome!" prefix


async def test_a_rate_shed_first_contact_creates_no_person_row(env, monkeypatch):
    # A shed message writes no person row: the greeting fires on the first ADMITTED message,
    # never a shed one (a shed first contact consumes nothing).
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _seed_config(env, greeting_template="Welcome!")
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())

    # addr-A drains the shared cap bucket; addr-B's FIRST message shares it and is shed.
    await turn_module.accept("twilio", "+15550001111", "visitor-a", "net-bucket", "hi", "PID-A")
    await _settle()
    shed = await turn_module.accept("twilio", "+15550001111", "visitor-b", "net-bucket", "hi", "PID-B")
    await _settle()
    shed_record = await _store().get_record(shed)
    assert shed_record is not None
    assert shed_record.answer == turn_module._SLOW_DOWN_TEXT
    # No person row for the shed first contact.
    person = await _person_store().get_person(
        _TARGET, door="channel", channel="twilio", our_identity="+15550001111", address="visitor-b"
    )
    assert person is None


async def test_greeting_due_first_contact_with_a_silent_tool_outcome_delivers_the_greeting_only(env, monkeypatch):
    _seed_config(env, target_kind="tool", target_name="pinger", greeting_template="Welcome!")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_route()), channel)
    _wire_tool(monkeypatch, lambda kw: None)  # a silent tool outcome

    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    record = await _store().get_record(mid)
    assert record is not None
    # The silent outcome became an answered greeting-only reply — a greeting is never dropped.
    assert record.answer_status == "answered"
    assert record.answer == "Welcome!"
    assert [n.message for n in channel.sends] == ["Welcome!"]


async def test_greeting_due_first_contact_with_an_error_outcome_keeps_the_greeting(env, monkeypatch):
    _seed_config(env, greeting_template="Welcome!")

    class _BoomAgent(Agent):
        tool_name = "boom"
        ToolInput = _EchoInput

        async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
            raise RuntimeError("agent blew up")

    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel(), agent=_BoomAgent())
    mid = await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    record = await _store().get_record(mid)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == f"Welcome!\n\n{turn_module._ERROR_ANSWER_TEXT}"


# -- the person in the tool payload + tool-target thread keying ---------------


async def test_tool_payload_off_carries_no_person_keys(env, monkeypatch):
    # Multichannel OFF (no config row): the payload the tool sees is exactly today's four
    # keys — no person_id / person_addresses. ``payload_expr="."`` echoes the whole payload.
    seen: list[dict] = []
    _wire(monkeypatch, FakeManager(_tool_route(payload_expr=".")), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: seen.append(kw) or "pong")
    await turn_module.accept("twilio", "+15550001111", "+2000", "+2000", "hi", "PID-1")
    await _settle()
    assert len(seen) == 1
    assert set(seen[0]) == {"message", "sender", "our_identity", "channel", "thread_id"}


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
        payload_expr=".",
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        execution_key_fingerprint="fp-1",
    )
    seen: list[dict] = []
    _wire(monkeypatch, FakeManager(api_tool_route))
    _wire_tool(monkeypatch, lambda kw: seen.append(kw) or "pong")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    client_address = turn_module._api_client_address("alice", "u7")
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


# -- the api door ------------------------------------------------------------


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
    invalid = [a for a in answers if a == turn_module._INVALID_CODE_TEXT]
    assert len(linked) == 1
    assert len(invalid) == 1


# -- the agent-turn drain the pairing passthrough routes ordinary text to -----


class _StreamAgent(Agent):
    """An agent whose ``astream`` yields exactly the events a test hands it — so the drain's
    structured/interrupt/empty branches are exercised directly."""

    tool_name = "stream"
    ToolInput = _EchoInput

    def __init__(self, *events) -> None:
        self._events = events

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):  # pragma: no cover
        raise NotImplementedError  # astream is overridden, so run is never reached

    async def astream(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        for event in self._events:
            yield event


def _resolved(outcome):
    """Assert a resolved (answered/error) outcome and return ``(answer_status, answer,
    error)`` — the shape the drain tests read."""
    assert isinstance(outcome, turn_module._ResolvedOutcome), outcome
    return outcome.answer_status, outcome.answer, outcome.error


async def test_agent_drain_serializes_structured_finals(env, monkeypatch):
    route = _api_route()
    cases = {
        "raw string": StructuredFinal(data="raw string"),  # str passes through
        '{"ok": true}': StructuredFinal(data={"ok": True}),  # json.dumps default
        '{"user_message":"m"}': StructuredFinal(data=_EchoInput(user_message="m")),  # model_dump_json
    }
    for expected, event in cases.items():
        monkeypatch.setattr(turn_module, "_agent_registry", lambda event=event: {"assistant": _StreamAgent(event)})
        status, answer, error = _resolved(await turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
        assert status == "answered"
        assert answer == expected
        assert error is None


async def test_agent_drain_raises_on_an_interrupt_becoming_an_error_outcome(env, monkeypatch):
    route = _api_route()
    agent = _StreamAgent(InterruptFinal(interrupt_id="i1", payload={"q": "?"}, reason="needs input"))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"assistant": agent})
    status, answer, error = _resolved(await turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
    assert status == "error"
    assert answer == turn_module._ERROR_ANSWER_TEXT
    assert error is not None
    assert "interrupt" in error


async def test_agent_drain_async_park_is_a_silent_outcome(env, monkeypatch):
    # The conversation door binds a completion tool around the turn, so an async ask_user
    # PARKS: the turn produces no reply now (a silent outcome) and the resumed answer is
    # delivered out of band by the completion continuation.
    route = _api_route()
    agent = _StreamAgent(SuspendedFinal(interaction_ids=["i1"], thread_id="t"))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"assistant": agent})
    outcome = await turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client")
    assert isinstance(outcome, turn_module._SilentOutcome)


async def test_agent_drain_empty_stream_is_an_empty_answer_error(env, monkeypatch):
    route = _api_route()
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"assistant": _StreamAgent()})  # no events
    status, _answer, error = _resolved(await turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
    assert status == "error"
    assert error == "agent produced an empty answer"


async def test_agent_turn_for_an_unregistered_agent_is_an_error(env, monkeypatch):
    route = _api_route()
    monkeypatch.setattr(turn_module, "_agent_registry", dict)
    status, _answer, error = _resolved(await turn_module._run_agent_turn(route, "hi", "bridge:x:y", "+client"))
    assert status == "error"
    assert error is not None
    assert "not registered" in error
