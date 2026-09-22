"""Fakes for the redis-backed conversation routing-row store.

``FakeRedis`` covers the STRING + SET operations the manager calls plus the two atomic
put/delete ``eval`` scripts. Single-threaded async, so each script runs atomically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.conversations import ConversationRoute
from tai42_contract.template import TemplatedText

from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations import cache as cache_module
from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import target_config as target_config_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations.managers.base_conversations_manager import (
    BaseConversationsManager,
    DoorFlipRefusedError,
)
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import agent_turn as agent_turn_module
from tai42_skeleton.conversations.turn import overlap as overlap_module
from tai42_skeleton.conversations.turn import schedule as schedule_module
from tai42_skeleton.conversations.turn import tool_turn as tool_turn_module
from tai42_skeleton.operations import conversations as ops

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


def rendered_user_message(user_message: TemplatedText | None) -> str:
    """The inline text an agent double receives as its ``user_message``.

    ``None`` is the no-message call shape and renders to the empty string. A
    :class:`TemplatedText` in the stored-``id`` form carries ``content is None``; the
    doubles resolve no stored resource, so that shape raises rather than reading as
    empty text and blessing a message that never arrived.
    """
    if user_message is None:
        return ""
    if user_message.content is None:
        raise ValueError(f"agent double received a stored-id templated text with no inline content: {user_message!r}")
    return user_message.content


@pytest.fixture(autouse=True)
def _clear_interactions_settings_cache():
    """Isolate the interactions store the conversation delete ops now consult.

    ``delete_conversation_{thread,person,route}`` cancel every async ``ask`` parked on a
    deleted thread via ``cancel_parks_for_thread`` → ``interactions_settings()``. That accessor
    is PROCESS-cached and many sibling suites set ``INTERACTIONS_REDIS_URL``, so a *configured*
    value cached by an earlier test would make a delete op here reach for a real Redis. Clearing
    that one cache before each conversations test makes each start from the unconfigured
    interactions store its own env implies (cancel then no-ops); a test that wants the
    interactions store wired does so explicitly through the helper's ``client_ctx`` seam.

    Cleared on BOTH sides: a conversations delete op re-populates the cache with THIS suite's
    *unconfigured* value, so clearing on teardown too keeps that value from leaking FORWARD into
    a sibling suite whose autouse env sets ``INTERACTIONS_REDIS_URL`` but whose read would still
    see the stale cached singleton (the symmetric hazard this fixture guards against inbound)."""
    from tai42_skeleton.interactions.settings import interactions_settings

    interactions_settings.cache_clear()
    yield
    interactions_settings.cache_clear()


class FakeRedis:
    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        #: The route thread indexes (keyspace 8) the put script reads its door-flip
        #: refusal from: ``{route_threads_key: thread count}``.
        self.threads_held: dict[str, int] = {}

    # -- direct surface the manager reads ------------------------------------
    async def get(self, key: str) -> str | None:
        return self._strings.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._strings.get(k) for k in keys]

    async def smembers(self, key: str) -> set[str]:
        return set(self._sets.get(key, set()))

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        """Emulate the atomic put/delete Lua scripts by their marker comment.

        Signature-compatible with ``redis.eval(script, numkeys, *keys, *args)``.
        """
        if "conversations:route:put:atomic" in script:
            names_key, route_key, threads_key, route_name, route_json, door = keys_and_args
            existing = self._strings.get(route_key)
            if existing is not None:
                stored_door = json.loads(existing)["door"]
                held = self.threads_held.get(threads_key, 0)
                if stored_door != door and held > 0:
                    return [held, stored_door]
            self._strings[route_key] = route_json
            self._sets.setdefault(names_key, set()).add(route_name)
            return 1 if existing is not None else 0
        if "conversations:route:delete:atomic" in script:
            names_key, route_key, route_name = keys_and_args
            removed = 1 if self._strings.pop(route_key, None) is not None else 0
            self._sets.get(names_key, set()).discard(route_name)
            return removed
        raise NotImplementedError("FakeRedis.eval only emulates the put/delete route scripts")


def make_client_ctx(fake: FakeRedis):
    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    return _ctx


# -- routing-operation test doubles + the standard happy-path environment ---------------------
#
# Shared by the route-CRUD and thread/person-delete operation suites: a dict-backed manager that
# admits ``_require_backend``, the agent/tool registries the create door checks a target against,
# and the ``wired`` fixture that binds them alongside a pass-role key bind.


class _DictManager(BaseConversationsManager):
    """A dict-backed routing-row store standing in for the redis manager (it is NOT the
    in-memory 501 manager, so ``_require_backend`` admits it).

    It mirrors each row into the record store's redis, where the real manager keeps it:
    the record create reads the routing row there to decide whether the route still routes,
    and the real ``put_route`` refuses a door flip in the same step as the write, so this
    stand-in owes the same refusal."""

    def __init__(self, redis: FakeRecordRedis) -> None:
        super().__init__(ConversationsSettings())
        self.rows: dict[str, ConversationRoute] = {}
        self._redis = redis

    async def put_route(self, route: ConversationRoute) -> bool:
        existing = self.rows.get(route.route_name)
        if existing is not None and existing.door != route.door:
            held = await self._redis.zcard(self.settings.route_threads_key(route.route_name))
            if held:
                raise DoorFlipRefusedError(route.route_name, existing.door, route.door, held)
        created = route.route_name not in self.rows
        self.rows[route.route_name] = route
        self._redis.seed_route(route.route_name)
        return created

    async def get_route(self, route_name: str) -> ConversationRoute | None:
        return self.rows.get(route_name)

    async def delete_route(self, route_name: str) -> bool:
        self._redis.drop_route(route_name)
        return self.rows.pop(route_name, None) is not None

    async def list_routes(self) -> dict[str, ConversationRoute]:
        return dict(self.rows)


class _AgentInput(BaseModel):
    user_message: str = ""


class _MemoryAgent(Agent):
    """An agent that holds thread memory — implements ``append_thread_messages``, so it can
    serve manual mode."""

    tool_name = "memory"
    ToolInput = _AgentInput

    async def run(self, *, user_message: TemplatedText | None = None, **kwargs):
        return ""

    async def append_thread_messages(self, *, thread_id, messages, **kwargs) -> None:
        return None


class _MemorylessAgent(Agent):
    """An agent that leaves ``append_thread_messages`` the ABC default, so it cannot serve
    manual mode."""

    tool_name = "memoryless"
    ToolInput = _AgentInput

    async def run(self, *, user_message: TemplatedText | None = None, **kwargs):
        return ""


class _FakeAgents:
    def __init__(self, agents: dict[str, Agent]) -> None:
        self._agents = agents

    def all_agents(self) -> dict[str, Agent]:
        return dict(self._agents)


class _OpsFakeTools:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    async def get_tool(self, key: str) -> object:
        from tai42_skeleton.tools.binding import UnknownToolError

        if key not in self._names:
            raise UnknownToolError(key)
        return object()


class _FakeResourceManager:
    """Renders a route jq slot: inline ``content`` verbatim, or a stored ``id`` from a
    per-id map — an unmapped id is the loud not-found the real manager raises."""

    def __init__(self, by_id: dict[str, str] | None = None) -> None:
        self._by_id = by_id or {}

    async def render_templated_text(self, text, locale=None):
        if text.id is not None:
            from tai42_skeleton.template.resource_manager import TemplateNotFoundError

            if text.id not in self._by_id:
                raise TemplateNotFoundError(f"no stored resource {text.id!r}")
            return self._by_id[text.id]
        assert text.content is not None
        return text.content


class _FakeStorage:
    def __init__(self, resource_manager: _FakeResourceManager) -> None:
        self.resource_manager = resource_manager


class _OpsFakeApp:
    def __init__(self, agents: dict[str, Agent], tools: set[str], by_id: dict[str, str] | None = None) -> None:
        self.agents = _FakeAgents(agents)
        self.tools = _OpsFakeTools(tools)
        self._target_validator_registry = TargetBindValidatorRegistry()
        self.conversations = ConversationsFacet(self)  # pyright: ignore[reportArgumentType]
        self.storage = _FakeStorage(_FakeResourceManager(by_id))


@pytest.fixture
def record_redis(monkeypatch) -> FakeRecordRedis:
    """The answer/record store's redis, behind the ops that reach the thread indexes."""
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    fake = FakeRecordRedis()
    ctx = make_record_client_ctx(fake)
    monkeypatch.setattr(records_module, "client_ctx", ctx)
    monkeypatch.setattr(thread_lease_module, "client_ctx", ctx)
    return fake


@pytest.fixture
def wired(monkeypatch, record_redis):
    """Wire a dict-backed manager, a pass-role bind that returns a fingerprint, an agent
    registry holding ``relay`` and a tool registry holding ``echo-tool`` — the standard
    happy-path environment."""
    manager = _DictManager(record_redis)
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: manager)

    async def _bindable(caller, execution_key):
        return "fp-derived"

    async def _caller():
        return object()

    monkeypatch.setattr(ops, "assert_execution_key_bindable", _bindable)
    monkeypatch.setattr(ops, "resolve_caller", _caller)

    from tai42_contract.app import tai42_app

    from tai42_skeleton.app import instance

    app = _OpsFakeApp(
        {"relay": _MemoryAgent(), "mute": _MemorylessAgent()},
        {"echo-tool"},
        by_id={"route-payload": "{message: .message}", "route-reply": ".result.reply // null"},
    )
    monkeypatch.setattr(instance, "app", app, raising=False)
    # The create door renders a tool route's payload/reply jq slots through the bound
    # resource manager before compiling them, so bind the same fake as ``tai42_app``.
    with tai42_app.bound(app):
        yield manager


# -- the parked-ask cascade harness ----------------------------------------------------------
#
# A conversation thread/person/route delete must cascade-cancel every async ``ask`` parked
# on the affected thread, or the deletion orphans the park: its expiry reaper later fires a
# continuation into the now-deleted thread and its channel correlation stays muted until the
# deadline. These wire the ``ask`` interactions store the delete-op cascade reaches and
# assert a park's state, its ``pending:expiry`` member and the reverse index after the op.


@pytest.fixture
def interactions_parks(monkeypatch):
    """Wire the ``ask`` interactions store the delete-op cascade reaches to a fake
    Redis, and return ``(store, fake)`` so a test can seed a park bound to a thread and
    assert it was cancelled. Mirrors the async-park store harness the interactions suite
    uses (fakeredis via the helper's ``client_ctx`` seam)."""
    from tai42_skeleton.interactions import helper as interactions_helper
    from tai42_skeleton.interactions.settings import InteractionsSettings
    from tai42_skeleton.interactions.store import InteractionStore

    from .._fakes.interactions_redis import FakeRedis as InteractionsFakeRedis

    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    fake = InteractionsFakeRedis()

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    settings = InteractionsSettings()
    monkeypatch.setattr(interactions_helper, "client_ctx", _ctx)
    monkeypatch.setattr(interactions_helper, "interactions_settings", lambda: settings)
    return InteractionStore(settings.key_prefix), fake


async def _seed_park(store, fake, *, interaction_id: str, group_id: str, thread_id: str) -> None:
    """Persist one async park bound to ``thread_id`` in the fake interactions store — the
    exact ``add`` the ``ask`` async branch performs, carrying the thread id."""
    from datetime import UTC, datetime, timedelta

    from tai42_contract.interactions import AnswerFormat, InteractionRequest

    now = datetime.now(UTC)
    expiry = now + timedelta(hours=1)
    request = InteractionRequest(
        interaction_id=interaction_id,
        group_id=group_id,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(interaction_id),
        created_at=now,
        timeout_at=expiry,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=expiry,
    )
    await store.add(fake, request, idle_ttl=86400, continuation_fingerprint="fp-1", thread_id=thread_id)


async def _assert_park_cancelled(store, fake, *, interaction_id: str, thread_id: str) -> None:
    from datetime import UTC, datetime, timedelta

    # The park's state is gone, its expiry member dropped (reaper fires nothing for it), and
    # the thread reverse index cleared.
    assert await store.get_state(fake, interaction_id) is None
    assert interaction_id not in await store.due_expiries(fake, datetime.now(UTC) + timedelta(days=1))
    assert await fake.smembers(store.thread_parks_key(thread_id)) == set()


# -- shared turn-engine test harness -----------------------------------------
# The fakes, route builders, wiring and settle helpers the turn-split test modules share.
# Turn symbols are patched at their owning submodule so a caller reading the name through
# that module at call time sees the double.

_TURN_LOGGER = "tai42_skeleton.conversations.turn"


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
    """Renders a route jq slot: inline ``content`` verbatim, or a stored ``id`` from a
    per-id map — an unmapped id is the loud not-found the real manager raises."""

    def __init__(self, by_id: dict[str, str] | None = None) -> None:
        self._by_id = by_id or {}

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        if text.id is not None:
            from tai42_skeleton.template.resource_manager import TemplateNotFoundError

            if text.id not in self._by_id:
                raise TemplateNotFoundError(f"no stored resource {text.id!r}")
            return self._by_id[text.id]
        assert text.content is not None
        return text.content


class _FakeTemplateStorage:
    def __init__(self, resource_manager: _FakeTemplateResourceManager) -> None:
        self.resource_manager = resource_manager


class _FakeTemplateApp:
    def __init__(self, by_id: dict[str, str] | None = None) -> None:
        self.storage = _FakeTemplateStorage(_FakeTemplateResourceManager(by_id))


def _channel_route(
    route_name: str = "line",
    our_identity: str = "+15550001111",
    *,
    turns_per_hour_override: int | None = None,
    error_reply_text: str | None = None,
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity=our_identity,
        execution_key_fingerprint="fp-1",
        turns_per_hour_override=turns_per_hour_override,
        error_reply_text=error_reply_text,
    )


def _api_route(
    route_name: str = "chat",
    *,
    turns_per_hour_override: int | None = None,
    error_reply_text: str | None = None,
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        execution_key_fingerprint="fp-1",
        turns_per_hour_override=turns_per_hour_override,
        error_reply_text=error_reply_text,
    )


def _api_route_no_callback(route_name: str = "chat") -> ConversationRoute:
    """A poll-only api route: no callback declared, so it carries no signing secret and its
    answer is read back from the poll door."""
    return ConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        callback_url=None,
        callback_secret=None,
        execution_key_fingerprint="fp-1",
    )


def _expr(text: str | None) -> TemplatedText | None:
    """A route jq slot as an inline templated text, or ``None`` when omitted."""
    return TemplatedText(content=text) if text is not None else None


def _tool_channel_route(
    route_name: str = "tool-line",
    our_identity: str = "+15550001111",
    *,
    target_name: str = "echo-tool",
    payload_expr: str | None = None,
    reply_expr: str | None = None,
    error_reply_text: str | None = None,
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="tool",
        target_name=target_name,
        payload_expr=_expr(payload_expr),
        reply_expr=_expr(reply_expr),
        execution_key="svc",
        channel="twilio",
        our_identity=our_identity,
        execution_key_fingerprint="fp-1",
        error_reply_text=error_reply_text,
    )


def _tool_api_route(
    route_name: str = "tool-api",
    *,
    target_name: str = "echo-tool",
    payload_expr: str | None = None,
    reply_expr: str | None = None,
    error_reply_text: str | None = None,
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="tool",
        target_name=target_name,
        payload_expr=_expr(payload_expr),
        reply_expr=_expr(reply_expr),
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        execution_key_fingerprint="fp-1",
        error_reply_text=error_reply_text,
    )


class _FakeTools:
    """A tool registry whose ``run_tool`` returns whatever a supplied callable produces from
    the dispatched kwargs — the stateless dispatch the tool-target turn drives."""

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[dict] = []

    async def run_tool(self, key: str, arguments: dict, *, offload_sync: bool = False):
        self.calls.append({"key": key, "arguments": arguments, "offload_sync": offload_sync})
        return self.fn(arguments)


def _wire_tool(monkeypatch, fn) -> _FakeTools:
    tools = _FakeTools(fn)
    monkeypatch.setattr(accessors_module, "_tools", lambda: tools)
    return tools


@asynccontextmanager
async def _fake_bind(execution_key, *, bound_fingerprint):
    yield CallerIdentity(user_id=execution_key)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    fake = FakeRecordRedis()
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(ledger_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(target_config_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(mode_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(thread_lease_module, "client_ctx", make_record_client_ctx(fake))
    # The overlap-cancel marker read/write shares the record redis (its own client_ctx seam).
    monkeypatch.setattr(overlap_module, "client_ctx", make_record_client_ctx(fake))
    # Stub the execution-identity authorization seam so the bridge is tested in isolation.
    # ``bind_execution_identity`` is read at call time by both the agent and tool turn modules.
    monkeypatch.setattr(agent_turn_module, "bind_execution_identity", _fake_bind)
    monkeypatch.setattr(tool_turn_module, "bind_execution_identity", _fake_bind)

    async def _allow(identity, agent_name, **kwargs):
        return None

    monkeypatch.setattr(agent_turn_module, "authorize_execution_agent_run", _allow)
    return fake


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


async def _settle(timeout: float = 2.0) -> None:
    """Let the spawned turn, delivery and grace tasks run to completion."""
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


def _wire(
    monkeypatch,
    manager: FakeManager,
    channel: FakeChannel | None = None,
    *,
    template_by_id: dict[str, str] | None = None,
) -> None:
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    # Every turn submodule reads the conversations manager through the cache module, and
    # ``mode.default_mode`` resolves a person thread's spanned routes through the same lazy
    # accessor, so the fake manager answers everywhere through this one patch point.
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: manager)
    # The turn renders a tool route's payload/reply jq slots through the bound resource
    # manager immediately before evaluating them; a fake renders inline slots verbatim and a
    # stored id from ``template_by_id``.
    monkeypatch.setattr(tool_turn_module, "tai42_app", _FakeTemplateApp(template_by_id))
    if channel is not None:
        monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))


def _accepting_callback():
    async def _post(url, body, signature, timeout_seconds):
        return 200

    return _post


def _completion_warnings(caplog, completion_id: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == _TURN_LOGGER and completion_id in record.getMessage()
    ]


class MemoryAgent(Agent):
    """An agent that accumulates the messages it has seen per ``thread_id`` and answers
    with the running history — so a second turn on the same thread proves the bridge
    handed it the SAME ``thread_id`` (the memory key)."""

    tool_name = "memo"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.threads: dict[str, list[str]] = {}

    async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
        text = rendered_user_message(user_message)
        history = self.threads.setdefault(thread_id or "", [])
        history.append(text)
        return " | ".join(history)


async def _all_record_ids(store: ConversationRecordStore) -> list[str]:
    return sorted(r.message_id for r in await store.list_by_status(frozenset(DeliveryStatus)))


# The channel-door tool payload with no params — the byte-identical baseline every
# ``None``/empty-params turn must reproduce exactly (``payload_expr="."`` passes it whole).
_BASELINE_CHANNEL_PAYLOAD = {
    "message": "hi",
    "sender": "+15550002222",
    "our_identity": "+15550001111",
    "channel": "twilio",
    "thread_id": "bridge:tool-line:+15550002222",
}


class BlockingAgent(Agent):
    """An agent that holds its turn open until released, so a second message for the same
    thread meets a genuinely full per-thread FIFO."""

    tool_name = "block"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
        text = rendered_user_message(user_message)
        self.calls.append(text)
        self.entered.set()
        await self.release.wait()
        return f"echo: {text}"


def _intake_record(message_id: str, provider_message_id: str):
    """An ``accepted`` record with no outcome — what a worker leaves behind when it stops
    between accepting a message and finishing its turn."""
    from tai42_skeleton.conversations.models import ConversationRecord

    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        provider_message_id=provider_message_id,
        origin="client",
        inbound_text=f"ask {message_id}",
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=now,
        updated_at=now,
    )


async def _create_stranded_intake(store: ConversationRecordStore, fake: FakeRecordRedis, message_id: str) -> None:
    """Persist an intake record whose intake lease has LAPSED — what a worker that died
    mid-turn leaves behind, and the only shape the re-drive may adopt."""
    await store.create_record(_intake_record(message_id, "PID1"), intake_token="dead-worker")
    key = ConversationsSettings().record_key(message_id)
    fields = await fake.hgetall(key)
    fake.seed_hash(key, fields | {"intake_claim": f"dead-worker:{time.time() - 1}"})


async def _intake_lease(fake: FakeRecordRedis, message_id: str) -> str:
    return (await fake.hgetall(ConversationsSettings().record_key(message_id)))["intake_claim"]
