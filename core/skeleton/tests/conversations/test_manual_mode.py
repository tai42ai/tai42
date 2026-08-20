"""Manual-mode inbound: the target turn is suppressed, an agent target has the inbound
appended to its checkpoint, a tool target has nothing appended, no turn runs, and the record
terminalises silent (channel door) or delivers the silent marker (api door). An append that
fails is a loud error outcome. Pairing turns stay live under manual mode.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.conversations import ConversationRoute, TargetConversationConfig

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
from tai42_skeleton.conversations.mode import ConversationModeStore
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


class _EchoInput(BaseModel):
    user_message: str = ""


class ManualAgent(Agent):
    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self, *, append_fails: bool = False) -> None:
        self.runs: list[tuple[str, str | None]] = []
        self.appended: list[tuple[str, list[dict[str, str]]]] = []
        self._append_fails = append_fails

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        self.runs.append((user_message, thread_id))
        return f"echo: {user_message}"

    async def append_thread_messages(self, *, thread_id: str, messages, **kwargs) -> None:
        if self._append_fails:
            raise RuntimeError("checkpoint write refused")
        self.appended.append((thread_id, messages))


class MemorylessAgent(Agent):
    """An agent that leaves ``append_thread_messages`` the ABC default (raises
    ``NotImplementedError`` if ever called), so it holds no thread memory to feed."""

    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.runs: list[tuple[str, str | None]] = []

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        self.runs.append((user_message, thread_id))
        return f"echo: {user_message}"


class ContextModeAgent(Agent):
    """An agent that, mid-run, calls the ``set_conversation_mode`` feature body — exercising
    that the turn-scoped bridge context reaches an in-process tool call the agent makes. It
    holds thread memory (implements ``append_thread_messages``), so flipping itself to manual
    is permitted."""

    tool_name = "echo"
    ToolInput = _EchoInput

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        from tai42_skeleton.conversations.mode import set_current_thread_mode

        await set_current_thread_mode("manual")
        return "handing you to a human"

    async def append_thread_messages(self, *, thread_id: str, messages, **kwargs) -> None:
        return None


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


class _FakeApp:
    def __init__(self, channel: FakeChannel) -> None:
        self.channels = _FakeChannels(channel)


class _FakeChannels:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    def get(self, name: str) -> FakeChannel:
        return self._channel


def _channel_route(initial_mode: str = "agent", *, target_kind: str = "agent") -> ConversationRoute:
    return ConversationRoute(
        route_name="line",
        door="channel",
        target_kind=target_kind,  # pyright: ignore[reportArgumentType]
        target_name="echo",
        payload_expr=None,
        reply_expr=None,
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        initial_mode=initial_mode,  # pyright: ignore[reportArgumentType]
        execution_key_fingerprint="fp-1",
    )


def _api_route(initial_mode: str = "manual") -> ConversationRoute:
    return ConversationRoute(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        initial_mode=initial_mode,  # pyright: ignore[reportArgumentType]
        execution_key_fingerprint="fp-1",
    )


@asynccontextmanager
async def _fake_bind(execution_key, *, bound_fingerprint):
    yield CallerIdentity(user_id=execution_key)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    fake = FakeRecordRedis()
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
        monkeypatch.setattr(module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(turn_module, "bind_execution_identity", _fake_bind)

    async def _allow(identity, agent_name, **kwargs):
        return None

    monkeypatch.setattr(turn_module, "authorize_execution_agent_run", _allow)
    return fake


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


async def _settle(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = [t for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            if not any(not t.done() for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS)):
                return
        await asyncio.wait(tasks, timeout=0.05)


def _wire(monkeypatch, manager: FakeManager, agent: Agent | None = None, channel: FakeChannel | None = None) -> None:
    monkeypatch.setattr(turn_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    if agent is not None:
        monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    if channel is not None:
        monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))


# -- channel door ------------------------------------------------------------


async def test_manual_channel_agent_appends_the_inbound_and_runs_no_turn(env, monkeypatch):
    agent = ManualAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="manual")), agent, channel)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "help", "PID1")
    await _settle()

    # No agent turn ran; the inbound was appended as a user message to the thread's memory.
    assert agent.runs == []
    assert agent.appended == [("bridge:line:+15550002222", [{"role": "user", "content": "help"}])]
    # The record is terminal silent and nothing was sent on the channel.
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.origin == "client"
    assert channel.sends == []


async def test_manual_channel_tool_appends_nothing_and_dispatches_no_tool(env, monkeypatch):
    dispatched: list = []

    class _Tools:
        async def run_tool(self, key, arguments, *, offload_sync=False):
            dispatched.append((key, arguments))
            return "unexpected"

    monkeypatch.setattr(turn_module, "_tools", lambda: _Tools())
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="manual", target_kind="tool")), channel=channel)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "help", "PID1")
    await _settle()

    assert dispatched == []
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []


async def test_manual_channel_memoryless_agent_appends_nothing_and_records_silent(env, monkeypatch):
    # Manual is valid for EVERY target: a memoryless agent (leaves append_thread_messages the
    # ABC default) has no thread memory to feed, so its manual-mode inbound records silently —
    # no append is attempted (the ABC default would raise), and there is NO error outcome.
    agent = MemorylessAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="manual")), agent, channel)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "help", "PID1")
    await _settle()

    assert agent.runs == []
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer_status is None
    assert record.error is None
    assert channel.sends == []


async def test_person_thread_manual_fold_to_a_memoryless_target_records_silent_not_error(env, monkeypatch):
    # A LINKED person's aggregated thread folds to manual when ANY spanned route defaults
    # manual. Reaching the route's MEMORYLESS agent target that way previously errored on every
    # message (an append it cannot serve); now the inbound records silent, no append, no error.
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    person = Person(
        person_id="p9",
        target_kind="agent",
        target_name="echo",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["line"],
                channel="twilio",
                our_identity="+15550001111",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
            PersonAddress(
                door="channel",
                routes=["line-b"],
                channel="whatsapp",
                our_identity="+15550003333",
                address="+2000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )

    class _FakePersonStore:
        def __init__(self, settings) -> None:
            pass

        async def get_by_id(self, person_id: str):
            return person if person_id == "p9" else None

    monkeypatch.setattr(persons_module, "ConversationPersonStore", _FakePersonStore)
    line_b = ConversationRoute(
        route_name="line-b",
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="whatsapp",
        our_identity="+15550003333",
        initial_mode="manual",
        execution_key_fingerprint="fp-1",
    )
    agent = MemorylessAgent()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="agent"), line_b), agent)
    route = _channel_route(initial_mode="agent")
    person_thread = "bridge:@person:p9"
    intake = turn_module._new_record(
        route=route,
        message_id="pm1",
        thread_id=person_thread,
        client_address="+1000",
        caller_principal=None,
        provider_message_id="PID-person",
        inbound_text="help",
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    await _store().create_record(intake, intake_token="tok")

    completed = await turn_module._complete_turn(route=route, intake=intake, text="help")

    # The folded manual mode suppressed the turn; the memoryless target fed nothing and the
    # record is terminal silent, never an error.
    assert completed.delivery_status is DeliveryStatus.SILENT
    assert agent.runs == []
    stored = await _store().get_record("pm1")
    assert stored is not None
    assert stored.delivery_status is DeliveryStatus.SILENT
    assert stored.error is None


async def test_manual_mode_via_a_thread_override_over_an_agent_default_route(env, monkeypatch):
    agent = ManualAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="agent")), agent, channel)
    # The route defaults to agent, but a per-thread override flips this one thread to manual.
    await ConversationModeStore(ConversationsSettings()).set_mode("bridge:line:+15550002222", "manual")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "help", "PID1")
    await _settle()

    assert agent.runs == []
    assert len(agent.appended) == 1
    assert channel.sends == []


async def test_agent_default_route_still_runs_the_turn(env, monkeypatch):
    # The default is agent mode: the turn runs and answers, byte-identical to today.
    agent = ManualAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="agent")), agent, channel)

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "help", "PID1")
    await _settle()

    assert agent.runs == [("help", "bridge:line:+15550002222")]
    assert agent.appended == []
    assert channel.sends[0].message == "echo: help"


async def test_manual_channel_append_failure_is_a_loud_error_outcome(env, monkeypatch):
    agent = ManualAgent(append_fails=True)
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="manual")), agent, channel)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "help", "PID1")
    await _settle()

    # An append that fails is never a silent drop: the record takes the error outcome and the
    # client-safe error text is delivered.
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert "manual-mode append error" in (record.error or "")
    assert len(channel.sends) == 1


# -- api door ----------------------------------------------------------------


async def test_manual_api_agent_delivers_the_silent_marker(env, monkeypatch):
    agent = ManualAgent()
    _wire(monkeypatch, FakeManager(_api_route(initial_mode="manual")), agent)

    result = await turn_module.submit_api_message("chat", "u1", "help", "caller-1", wait_seconds=5)
    await _settle()

    # The sync-wait returns the silent marker (no answer text), the turn never ran, and the
    # inbound was appended to the thread's memory.
    assert result.answer is not None
    assert result.answer.status == "silent"
    assert result.answer.answer is None
    assert agent.runs == []
    assert agent.appended == [(result.thread_id, [{"role": "user", "content": "help"}])]


# -- pairing stays live ------------------------------------------------------


async def test_manual_mode_keeps_a_pairing_turn_live(env, monkeypatch):
    # A /link on a multichannel target still mints a code and replies, even in manual mode:
    # pairing is a platform control turn, dispatched on its own path, never the target turn.
    agent = ManualAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="manual")), agent, channel)
    # Seed the multichannel config row directly (the record fake carries no config put Lua).
    settings = ConversationsSettings()
    config = TargetConversationConfig(target_kind="agent", target_name="echo", multichannel=True)
    env._strings[settings.target_config_key("agent", "echo")] = config.model_dump_json()

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "/link", "PID1")
    await _settle()

    # The pairing reply went out (a code was minted and sent), and the target turn never ran.
    assert agent.runs == []
    assert len(channel.sends) == 1
    assert "pairing code" in channel.sends[0].message.lower()


# -- the in-turn builtin: set_conversation_mode reads the bridge context -----


async def test_agent_calling_set_conversation_mode_flips_the_current_thread(env, monkeypatch):
    # Drive a real agent turn through accept -> _run_agent_turn (which establishes the
    # turn-scoped bridge context) -> the agent's run, which calls the set_conversation_mode
    # feature body. The context propagates in-process to that call, so the mode override lands
    # on the very thread the turn ran on — the agent supplied no identifiers.
    from tai42_skeleton.conversations.mode import ConversationModeStore

    agent = ContextModeAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(initial_mode="agent")), agent, channel)

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "help", "PID1")
    await _settle()

    assert await ConversationModeStore(ConversationsSettings()).get_mode("bridge:line:+15550002222") == "manual"
    # The agent's own answer still delivered — flipping the mode does not suppress this turn.
    assert channel.sends[0].message == "handing you to a human"
