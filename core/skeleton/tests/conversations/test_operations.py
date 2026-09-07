"""The conversation-route CRUD operations: create (pass-role bind + target-exists + jq
compile + callback-secret mint), get/list (secret withheld), delete, and the slug guard."""

from __future__ import annotations

import time
from typing import Any, cast

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations.managers.base_conversations_manager import (
    BaseConversationsManager,
    DoorFlipRefused,
)
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, ConflictError, NotFoundError, ValidationRejected

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


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
                raise DoorFlipRefused(route.route_name, existing.door, route.door, held)
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

    async def run(self, *, user_message: str = "", **kwargs):
        return ""

    async def append_thread_messages(self, *, thread_id, messages, **kwargs) -> None:
        return None


class _MemorylessAgent(Agent):
    """An agent that leaves ``append_thread_messages`` the ABC default, so it cannot serve
    manual mode."""

    tool_name = "memoryless"
    ToolInput = _AgentInput

    async def run(self, *, user_message: str = "", **kwargs):
        return ""


class _FakeAgents:
    def __init__(self, agents: dict[str, Agent]) -> None:
        self._agents = agents

    def all_agents(self) -> dict[str, Agent]:
        return dict(self._agents)


class _FakeTools:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    async def get_tool(self, key: str) -> object:
        from tai42_skeleton.tools.binding import UnknownToolError

        if key not in self._names:
            raise UnknownToolError(key)
        return object()


class _FakeApp:
    def __init__(self, agents: dict[str, Agent], tools: set[str]) -> None:
        self.agents = _FakeAgents(agents)
        self.tools = _FakeTools(tools)
        self._target_validator_registry = TargetBindValidatorRegistry()
        self.conversations = ConversationsFacet(self)  # pyright: ignore[reportArgumentType]


class _FakeRouteRequest:
    """The minimal request surface ``_extract_route_create`` reads: an async JSON body and
    the ``route_name`` path param — enough to drive the real HTTP-door extractor without a
    live server."""

    def __init__(self, body: dict, route_name: str) -> None:
        self._body = body
        self.path_params = {"route_name": route_name}

    async def json(self) -> dict:
        return self._body


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

    from tai42_skeleton.app import instance

    monkeypatch.setattr(
        instance,
        "app",
        _FakeApp({"relay": _MemoryAgent(), "mute": _MemorylessAgent()}, {"echo-tool"}),
        raising=False,
    )
    return manager


async def test_create_api_route_mints_and_shows_the_secret_once(wired):
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True
    assert result["callback_secret"]  # shown once here
    # The stored fingerprint is the one the bind derived, never a client value.
    assert wired.rows["chat"].execution_key_fingerprint == "fp-derived"
    assert wired.rows["chat"].callback_secret == result["callback_secret"]
    # The route view withholds the secret.
    assert "callback_secret" not in result["route"]


async def test_route_create_seam_binds_extractor_to_the_operation(wired):
    """The real HTTP-door binding: ``_extract_route_create`` -> ``op.func(**kwargs)`` exactly
    as ``adapter.py`` dispatches it. This is the seam the un-wired ``turns_per_hour_override``
    param broke: the extractor's ``model_dump()`` always emits the key, so an operation
    signature missing it TypeErrors -> 500 on EVERY route create.

    Once WITHOUT the key in the body (the default path that used to 500) and once WITH a
    positive override (proving it now persists to the stored row)."""
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app import instance
    from tai42_skeleton.operations.decorator import operation_metadata_of

    # The conversations router registers its routes against the ``tai42_app`` handle at
    # import time (as the runtime and the routers test conftest do), so bind a built app for
    # that one-shot import; the extractor and the operation themselves need no bound app to
    # run, and the operation still resolves its agents/manager through the ``wired`` fakes.
    with tai42_app.bound(instance.build_app()):
        from tai42_skeleton.routers.conversations import _extract_route_create

    op = operation_metadata_of(ops.create_conversation_route)
    base = {
        "door": "api",
        "target_kind": "agent",
        "target_name": "relay",
        "execution_key": "svc",
        "callback_url": "https://example.com/cb",
    }

    # Default path: body omits the override entirely; the extractor still emits the key and
    # the operation accepts it, storing ``None``.
    kwargs = await _extract_route_create(cast(Any, _FakeRouteRequest(dict(base), "chat")))
    result = await op.func(**kwargs)
    assert isinstance(result, dict)
    assert result["created"] is True
    assert wired.rows["chat"].turns_per_hour_override is None

    # Override path: a positive override rides the body through the seam and persists.
    kwargs_over = await _extract_route_create(
        cast(Any, _FakeRouteRequest({**base, "turns_per_hour_override": 6000}, "fast"))
    )
    result_over = await op.func(**kwargs_over)
    assert isinstance(result_over, dict)
    assert result_over["created"] is True
    stored = await wired.get_route("fast")
    assert stored is not None
    assert stored.turns_per_hour_override == 6000


async def test_route_create_seam_persists_error_reply_text(wired):
    """The same extractor -> operation -> store seam carries ``error_reply_text``: a body that
    sets it lands the custom guest-facing reply on the stored row, and a body that omits it
    stores ``None`` (the built-in default applies at turn time)."""
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app import instance
    from tai42_skeleton.operations.decorator import operation_metadata_of

    with tai42_app.bound(instance.build_app()):
        from tai42_skeleton.routers.conversations import _extract_route_create

    op = operation_metadata_of(ops.create_conversation_route)
    base = {
        "door": "api",
        "target_kind": "agent",
        "target_name": "relay",
        "execution_key": "svc",
        "callback_url": "https://example.com/cb",
    }

    # Default path: body omits the reply; the stored row carries ``None``.
    kwargs = await _extract_route_create(cast(Any, _FakeRouteRequest(dict(base), "chat")))
    result = await op.func(**kwargs)
    assert isinstance(result, dict)
    assert result["created"] is True
    assert wired.rows["chat"].error_reply_text is None

    # Custom path: a non-blank reply rides the body through the seam and persists verbatim.
    text = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    kwargs_custom = await _extract_route_create(
        cast(Any, _FakeRouteRequest({**base, "error_reply_text": text}, "spanish"))
    )
    result_custom = await op.func(**kwargs_custom)
    assert isinstance(result_custom, dict)
    assert result_custom["created"] is True
    stored = await wired.get_route("spanish")
    assert stored is not None
    assert stored.error_reply_text == text


async def test_create_defaults_initial_mode_to_agent_and_surfaces_it(wired):
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    # The default surfaces in the stored row and the public view.
    assert wired.rows["chat"].initial_mode == "agent"
    assert result["route"]["initial_mode"] == "agent"


async def test_create_stores_a_manual_initial_mode(wired):
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
        initial_mode="manual",
    )
    assert wired.rows["chat"].initial_mode == "manual"
    assert result["route"]["initial_mode"] == "manual"

    got = await ops.get_conversation_route("chat")
    assert got["initial_mode"] == "manual"


async def test_create_manual_on_a_memoryless_agent_is_allowed(wired):
    # Manual is valid for EVERY target: an agent that leaves append_thread_messages the ABC
    # default holds no thread memory to feed, so its manual-mode inbound records silently — the
    # create never refuses on target memory.
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="mute",
        execution_key="svc",
        callback_url="https://example.com/cb",
        initial_mode="manual",
    )
    assert result["created"] is True
    assert wired.rows["chat"].initial_mode == "manual"


async def test_create_manual_on_a_tool_target_is_allowed(wired):
    # A tool target holds no thread memory to append, so manual mode is always allowed for it.
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="tool",
        target_name="echo-tool",
        execution_key="svc",
        callback_url="https://example.com/cb",
        initial_mode="manual",
    )
    assert result["created"] is True
    assert wired.rows["chat"].initial_mode == "manual"


async def test_create_channel_route_carries_no_secret(wired):
    result = await ops.create_conversation_route(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
    )
    assert result["callback_secret"] is None
    assert wired.rows["line"].callback_secret is None


async def test_create_is_an_upsert(wired):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc2",
        callback_url="https://example.com/cb2",
    )
    assert result["created"] is False
    assert wired.rows["chat"].execution_key == "svc2"


async def test_a_channel_identity_is_stored_canonicalized(wired):
    # Inbound routing matches by equality on the canonical form, so the row is stored
    # canonicalized — a verbatim row would match nothing and hide duplicates.
    await ops.create_conversation_route(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        channel="twilio",
        our_identity="  +15550001111  ",
    )
    assert wired.rows["line"].our_identity == "+15550001111"


async def test_a_second_route_claiming_one_channel_identity_is_refused(wired):
    await ops.create_conversation_route(
        route_name="line-a",
        door="channel",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111 ",
    )
    # The same number under a different spelling is one identity, so the second route is
    # refused here rather than leaving the routing table unresolvable.
    with pytest.raises(BadRequestError, match="already routed by 'line-a'"):
        await ops.create_conversation_route(
            route_name="line-b",
            door="channel",
            target_kind="agent",
            target_name="relay",
            execution_key="svc",
            channel="twilio",
            our_identity="+15550001111",
        )
    assert "line-b" not in wired.rows


async def test_a_route_may_re_claim_its_own_channel_identity(wired):
    # The create door is an UPSERT: editing a row must not trip over the identity that row
    # itself already holds.
    for execution_key in ("svc", "svc2"):
        await ops.create_conversation_route(
            route_name="line",
            door="channel",
            target_kind="agent",
            target_name="relay",
            execution_key=execution_key,
            channel="twilio",
            our_identity="+15550001111",
        )
    assert wired.rows["line"].execution_key == "svc2"


async def test_the_same_identity_on_another_channel_is_a_different_route(wired):
    # The pair is (channel, identity): the same handle on a different medium is a
    # different destination, and both rows resolve.
    for route_name, channel in (("line", "twilio"), ("tg", "telegram")):
        await ops.create_conversation_route(
            route_name=route_name,
            door="channel",
            target_kind="agent",
            target_name="relay",
            execution_key="svc",
            channel=channel,
            our_identity="+15550001111",
        )
    assert set(wired.rows) == {"line", "tg"}


async def test_create_rejects_a_colon_channel_name(wired):
    # The channel name qualifies the dedupe and outbound-index keys ahead of the provider's
    # own id, so a ``:`` in it would move that boundary.
    with pytest.raises(BadRequestError, match="free of ':'"):
        await ops.create_conversation_route(
            route_name="line",
            door="channel",
            target_kind="agent",
            target_name="relay",
            execution_key="svc",
            channel="twi:lio",
            our_identity="+15550001111",
        )
    assert wired.rows == {}


async def test_create_rejects_unknown_agent(wired):
    with pytest.raises(NotFoundError, match="agent not found"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="agent",
            target_name="ghost",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_tool_route_validates_tool_and_compiles_exprs(wired):
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="tool",
        target_name="echo-tool",
        payload_expr="{message: .message, who: .sender}",
        reply_expr=".reply // null",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True
    row = wired.rows["chat"]
    assert row.target_kind == "tool"
    assert row.target_name == "echo-tool"
    assert row.payload_expr == "{message: .message, who: .sender}"
    assert row.reply_expr == ".reply // null"


async def test_create_rejects_unknown_tool(wired):
    with pytest.raises(NotFoundError, match="tool not found"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="tool",
            target_name="ghost-tool",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_rejects_invalid_payload_expr(wired):
    with pytest.raises(BadRequestError, match="invalid payload_expr"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="tool",
            target_name="echo-tool",
            payload_expr="{unterminated",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_rejects_invalid_reply_expr(wired):
    with pytest.raises(BadRequestError, match="invalid reply_expr"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="tool",
            target_name="echo-tool",
            reply_expr=".[",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_rejects_exprs_on_agent_target(wired):
    with pytest.raises(BadRequestError, match="no payload_expr/reply_expr"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="agent",
            target_name="relay",
            reply_expr=".reply",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_rejects_a_colon_route_name(wired):
    with pytest.raises(BadRequestError, match="slug"):
        await ops.create_conversation_route(
            route_name="bad:name",
            door="api",
            target_kind="agent",
            target_name="relay",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_bind_refusal_leaves_no_row(wired, monkeypatch):
    async def _refuse(caller, execution_key):
        raise BadRequestError("not yours")

    monkeypatch.setattr(ops, "assert_execution_key_bindable", _refuse)
    with pytest.raises(BadRequestError, match="not yours"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="agent",
            target_name="relay",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )
    assert "chat" not in wired.rows


async def test_get_withholds_the_secret_and_404s_unknown(wired):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    view = await ops.get_conversation_route("chat")
    assert "callback_secret" not in view
    assert view["route_name"] == "chat"
    with pytest.raises(NotFoundError):
        await ops.get_conversation_route("missing")


async def test_get_rejects_a_colon_route_name(wired):
    with pytest.raises(BadRequestError, match="slug"):
        await ops.get_conversation_route("bad:name")


async def test_list_withholds_secrets(wired):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    listed = await ops.list_conversation_routes()
    assert listed["total"] == 1
    assert all("callback_secret" not in item for item in listed["items"])


async def test_delete_removes_then_404s(wired):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert (await ops.delete_conversation_route("chat"))["removed"] is True
    with pytest.raises(NotFoundError):
        await ops.delete_conversation_route("chat")


async def test_delete_reclaims_the_routes_thread_indexes(wired, record_redis):
    # Neither thread index carries a TTL and the prune pass only walks LIVE routes, so a
    # delete that left them behind stranded them in redis forever.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    settings = ConversationsSettings()
    now = time.time()
    for index in range(2):
        await ConversationRecordStore(settings).create_record(
            ConversationRecord(
                message_id=f"m{index}",
                route_name="chat",
                door="api",
                thread_id=f"bridge:chat:alice/user-{index}",
                client_address=f"alice/user-{index}",
                caller_principal="alice",
                callback_url="https://example.com/cb",
                origin="client",
                inbound_text="ask",
                answer_status="answered",
                answer="the answer",
                delivery_status=DeliveryStatus.DELIVERED,
                created_at=now,
                updated_at=now,
            )
        )
    assert settings.route_threads_key("chat") in record_redis._zsets

    assert (await ops.delete_conversation_route("chat"))["removed"] is True

    assert settings.route_threads_key("chat") not in record_redis._zsets
    for index in range(2):
        assert settings.thread_index_key("chat", f"bridge:chat:alice/user-{index}") not in record_redis._zsets


async def test_an_interrupted_delete_is_finished_by_a_retry_instead_of_404ing(wired, record_redis, monkeypatch):
    # The reclamation runs AFTER the routing row is gone, so a socket timeout, a SIGTERM or
    # a redis blip mid-loop strands whatever it had not reached: nothing walks a name that
    # no longer routes. A retry that answered 404 would leave those keys unnameable forever.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_thread_on("chat", door="api", thread_id="bridge:chat:alice/user-1")
    settings = ConversationsSettings()
    inner = record_redis.zrem
    blown = False

    async def _blows_up_once(key, *members):
        nonlocal blown
        if not blown:
            blown = True
            raise TimeoutError("redis socket timeout")
        return await inner(key, *members)

    monkeypatch.setattr(record_redis, "zrem", _blows_up_once)

    with pytest.raises(TimeoutError):
        await ops.delete_conversation_route("chat")

    # The routing row is already gone, so no message can open a thread on the name...
    assert "chat" not in wired.rows
    # ...and the route's thread index survives, holding the work the run never reached.
    assert settings.route_threads_key("chat") in record_redis._zsets

    result = await ops.delete_conversation_route("chat")

    # Not a 404: the retry re-ran the reclamation and says the row was not this call's to
    # remove.
    assert result == {"removed": False, "route_name": "chat"}
    assert settings.route_threads_key("chat") not in record_redis._zsets
    assert settings.thread_index_key("chat", "bridge:chat:alice/user-1") not in record_redis._zsets


async def _seed_thread_on(route_name: str, *, door: str, thread_id: str) -> None:
    """One delivered record on ``route_name``, so the route's thread index holds a thread."""
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=f"m-{thread_id}",
            route_name=route_name,
            door=door,  # type: ignore[arg-type]
            thread_id=thread_id,
            client_address="alice/user-1",
            caller_principal="alice" if door == "api" else None,
            callback_url="https://example.com/cb" if door == "api" else None,
            channel="twilio" if door == "channel" else None,
            our_identity="+15550001111" if door == "channel" else None,
            origin="client",
            inbound_text="ask",
            answer_status="answered",
            answer="the answer",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


async def test_flipping_the_door_of_a_route_that_holds_threads_is_refused(wired, record_redis):
    # The doors key their threads differently, so an api→channel flip cannot re-key the
    # threads the route already holds; it is refused rather than half-applied.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_thread_on("chat", door="api", thread_id="bridge:chat:alice/user-1")

    with pytest.raises(BadRequestError, match="holds 1 thread"):
        await ops.create_conversation_route(
            route_name="chat",
            door="channel",
            target_kind="agent",
            target_name="relay",
            execution_key="svc",
            channel="twilio",
            our_identity="+15550001111",
        )
    # Refused BEFORE any write: the row still routes exactly as it did.
    assert wired.rows["chat"].door == "api"


async def test_a_thread_opened_during_the_edit_still_refuses_the_door_flip(wired, record_redis, monkeypatch):
    # A count read a few awaits before the write is no guard at all: the edit resolves its
    # target, compiles its exprs and binds its execution key in between, and a first message
    # landing in that window opens the very thread the refusal exists to protect — after
    # which the flip lands anyway, and the revert is refused too.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )

    async def _bind_while_a_first_message_lands(caller, execution_key):
        await _seed_thread_on("chat", door="api", thread_id="bridge:chat:alice/user-1")
        return "fp-derived"

    monkeypatch.setattr(ops, "assert_execution_key_bindable", _bind_while_a_first_message_lands)

    with pytest.raises(BadRequestError, match="holds 1 thread"):
        await ops.create_conversation_route(
            route_name="chat",
            door="channel",
            target_kind="agent",
            target_name="relay",
            execution_key="svc",
            channel="twilio",
            our_identity="+15550001111",
        )
    assert wired.rows["chat"].door == "api"


async def test_the_door_of_a_route_holding_no_thread_is_still_editable(wired, record_redis):
    # The refusal is about orphaning threads, not about the door being immutable.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    result = await ops.create_conversation_route(
        route_name="chat",
        door="channel",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
    )
    assert result["created"] is False
    assert wired.rows["chat"].door == "channel"


async def test_an_edit_that_keeps_the_door_is_untouched_by_the_guard(wired, record_redis):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_thread_on("chat", door="api", thread_id="bridge:chat:alice/user-1")

    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc2",
        callback_url="https://example.com/cb2",
    )
    assert result["created"] is False
    assert wired.rows["chat"].execution_key == "svc2"


async def test_unclaimed_channel_identity_rejects_a_blank_identity(wired):
    # The identity guard canonicalizes before comparing; a value blank once trimmed keys no
    # route, so it is refused as a 400 rather than stored unresolvable.
    with pytest.raises(BadRequestError, match="invalid our_identity"):
        await ops._unclaimed_channel_identity(wired, route_name="line", channel="twilio", our_identity="   ")


async def test_operations_501_without_a_backend(monkeypatch):
    from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
    from tai42_skeleton.operations.errors import NotSupportedError

    monkeypatch.setattr(ops, "get_conversations_manager", lambda: InMemoryConversationsManager(ConversationsSettings()))
    with pytest.raises(NotSupportedError):
        await ops.list_conversation_routes()
    with pytest.raises(NotSupportedError):
        await ops.list_conversation_threads("chat")
    with pytest.raises(NotSupportedError):
        await ops.get_conversation_thread("chat", "bridge:chat:+15550001111")
    with pytest.raises(NotSupportedError):
        await ops.delete_conversation_thread("chat", "bridge:chat:+15550001111")


# -- item-level thread delete ----------------------------------------------------------------

_THREAD = "bridge:chat:+15550001111"
_PERSON_THREAD = "bridge:@person:p1"


class _RecordingSaver:
    """A checkpoint saver that records every thread ``adelete_thread`` was asked to forget."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class _FakeCheckpointRegistry:
    def __init__(self, saver: _RecordingSaver) -> None:
        self._saver = saver

    async def get_checkpointer(self, provider: str, conn_string: str | None) -> _RecordingSaver:
        return self._saver


class _FakeProviderSettings:
    checkpoint = "memory"
    checkpoint_conn_string = None


@pytest.fixture
def checkpoint_saver(monkeypatch) -> _RecordingSaver:
    """Stub the checkpoint access path the thread delete reaches, capturing the thread ids it
    asks the saver to forget — the agent's actual memory store."""
    import tai42_kit.llm.checkpoint.checkpoint_registry as registry_mod
    import tai42_kit.llm.settings as settings_mod

    saver = _RecordingSaver()
    monkeypatch.setattr(registry_mod, "checkpoint_registry", lambda: _FakeCheckpointRegistry(saver))
    monkeypatch.setattr(settings_mod, "llm_provider_settings", lambda: _FakeProviderSettings())
    return saver


async def _seed_record(*, message_id: str, route_name: str, thread_id: str) -> None:
    """One delivered record under ``route_name`` on ``thread_id`` — a distinct message id per
    call, so a thread spanning several routes gets a distinct record on each."""
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=thread_id,
            client_address="+15550001111",
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask",
            answer_status="answered",
            answer="the answer",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


async def _seed_accepted(*, message_id: str, route_name: str, thread_id: str) -> None:
    """One ``accepted`` intake record holding a live intake lease — a turn in flight."""
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=thread_id,
            client_address="+15550001111",
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask",
            delivery_status=DeliveryStatus.ACCEPTED,
            created_at=now,
            updated_at=now,
        ),
        intake_token="tok",
    )


async def test_delete_thread_clears_checkpoint_records_and_indexes(wired, record_redis, checkpoint_saver):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    # a. the agent checkpoint — adelete_thread asked to forget exactly this thread.
    assert checkpoint_saver.deleted == [_THREAD]
    # b. the answer record itself.
    assert settings.record_key("m0") not in record_redis._hashes
    # c. both thread indexes.
    assert settings.thread_index_key("chat", _THREAD) not in record_redis._zsets
    assert _THREAD not in record_redis._zsets.get(settings.route_threads_key("chat"), {})
    # Forgetting is absolute: a re-run of a now-empty id on its own route succeeds with
    # removed=0 and forgets the checkpoint again, never a 404.
    rerun = await ops.delete_conversation_thread("chat", _THREAD)
    assert rerun == {"removed": 0, "route_name": "chat", "thread_id": _THREAD}
    assert checkpoint_saver.deleted == [_THREAD, _THREAD]


async def test_delete_never_seen_thread_on_its_route_forgets_the_checkpoint_with_removed_zero(
    wired, record_redis, checkpoint_saver
):
    # Forgetting is absolute: an id on its own route that the index never held is not a 404 —
    # it succeeds with removed=0 and its checkpoint is deleted regardless, so an aged-out
    # thread whose records already lapsed is still forgotten in agent memory.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    result = await ops.delete_conversation_thread("chat", "bridge:chat:never")

    assert result == {"removed": 0, "route_name": "chat", "thread_id": "bridge:chat:never"}
    assert checkpoint_saver.deleted == ["bridge:chat:never"]


async def test_delete_thread_rejects_a_foreign_route_prefix_and_forgets_no_checkpoint(
    wired, record_redis, checkpoint_saver
):
    # A route-keyed id must carry the named route's ``bridge:{route_name}:`` prefix. An id of
    # another route (prefix mismatch) is a loud 400 — the guard stopping a delete on one route
    # from wiping another route's memory — and the checkpoint is never touched.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    with pytest.raises(BadRequestError, match="not a thread of route 'chat'"):
        await ops.delete_conversation_thread("chat", "bridge:other:+15550001111")
    assert checkpoint_saver.deleted == []


async def test_delete_thread_with_a_turn_in_flight_is_a_409(wired, record_redis, checkpoint_saver):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_accepted(message_id="live", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()

    with pytest.raises(ConflictError, match="turn in flight"):
        await ops.delete_conversation_thread("chat", _THREAD)
    # Refused before any teardown: the intake record stands and the checkpoint is untouched.
    assert checkpoint_saver.deleted == []
    assert settings.record_key("live") in record_redis._hashes
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets


async def test_delete_thread_waits_behind_an_in_flight_operator_send(wired, record_redis, checkpoint_saver):
    # The thread delete's teardown takes the thread's per-thread FIFO — the SAME lock an
    # operator send and an in-flight turn hold — so a delete cannot interleave an in-flight
    # operator send's checkpoint + record writes: it lands only once the holder releases.
    import asyncio

    from tai42_skeleton.conversations import caps as caps_module

    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)

    caps_module._CAPS_CACHE.clear()
    caps = caps_module.get_turn_caps()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_thread() -> None:
        caps.reserve_thread_slot(_THREAD)
        async with caps.run_reserved(_THREAD):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(_hold_the_thread())
    await asyncio.wait_for(holding.wait(), 1.0)

    delete = asyncio.create_task(ops.delete_conversation_thread("chat", _THREAD))
    # While the holder keeps the thread, the delete is blocked before any teardown: the
    # checkpoint is untouched.
    await asyncio.sleep(0.05)
    assert not delete.done()
    assert checkpoint_saver.deleted == []

    # The holder releases the thread; only now does the delete's teardown proceed.
    release.set()
    await holder
    result = await delete

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    assert checkpoint_saver.deleted == [_THREAD]


async def test_delete_thread_rechecks_the_live_intake_under_the_lock(wired, record_redis, checkpoint_saver):
    # The in-flight guard is checked AGAIN under the FIFO: an intake admitted after the cheap
    # outer check but before the delete holds the lock is caught in-lock and 409s, so its turn
    # — queued behind the delete's lock — never runs after the teardown and re-creates the
    # checkpoint. Deterministic simulation: a holder pins the lock, the delete passes the outer
    # check and blocks on it, an accepted intake is seeded while it waits, then the holder
    # releases and the delete's in-lock re-check finds the intake.
    import asyncio

    from tai42_skeleton.conversations import caps as caps_module

    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)

    caps_module._CAPS_CACHE.clear()
    caps = caps_module.get_turn_caps()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_thread() -> None:
        caps.reserve_thread_slot(_THREAD)
        async with caps.run_reserved(_THREAD):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(_hold_the_thread())
    await asyncio.wait_for(holding.wait(), 1.0)

    delete = asyncio.create_task(ops.delete_conversation_thread("chat", _THREAD))
    # The delete clears the outer check (no live intake yet) and blocks on the held lock: it is
    # alive and has torn nothing down.
    await asyncio.sleep(0.05)
    assert not delete.done()
    assert checkpoint_saver.deleted == []

    # A turn is admitted while the delete waits behind the lock.
    await _seed_accepted(message_id="slipped-in", route_name="chat", thread_id=_THREAD)

    release.set()
    await holder
    # The delete acquires the lock, re-checks, and refuses — from within the lock, never a
    # teardown that would strand the admitted turn's memory half-forgotten.
    with pytest.raises(ConflictError, match="turn in flight"):
        await delete
    assert checkpoint_saver.deleted == []
    settings = ConversationsSettings()
    assert settings.record_key("m0") in record_redis._hashes
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets


async def test_delete_thread_ignores_a_live_intake_on_another_thread(wired, record_redis, checkpoint_saver):
    # An accepted record holding a LIVE lease on a DIFFERENT thread is not a turn in flight on
    # this one, so the delete proceeds.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    await _seed_accepted(message_id="elsewhere", route_name="chat", thread_id="bridge:chat:+15559990000")

    result = await ops.delete_conversation_thread("chat", _THREAD)
    assert result["removed"] == 1
    assert checkpoint_saver.deleted == [_THREAD]


async def test_delete_thread_proceeds_when_the_target_thread_intake_lease_has_lapsed(
    wired, record_redis, checkpoint_saver
):
    # An accepted record ON THE TARGET THREAD whose intake lease has LAPSED is not a turn in
    # flight: its worker is gone, so the delete proceeds and forgets the memory. Pins the
    # ``expiry > now`` liveness comparison — inverting it would 409 this dead lease.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_accepted(message_id="lapsed", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()
    # Force the stored lease expiry into the past — the way records.py reads it is
    # ``float(expiry) > now``, so a past expiry is a dead lease.
    record_redis._hashes[settings.record_key("lapsed")]["intake_claim"] = f"tok:{time.time() - 3600}"

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    assert checkpoint_saver.deleted == [_THREAD]


async def test_delete_person_thread_clears_every_route_index(wired, record_redis, checkpoint_saver, monkeypatch):
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
            PersonAddress(
                door="channel",
                routes=["chat-b"],
                channel="whatsapp",
                our_identity="+2",
                address="+2000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person_id == person.person_id else None

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())

    settings = ConversationsSettings()
    record_redis.seed_route("chat-a")
    record_redis.seed_route("chat-b")
    await _seed_record(message_id="pa", route_name="chat-a", thread_id=_PERSON_THREAD)
    await _seed_record(message_id="pb", route_name="chat-b", thread_id=_PERSON_THREAD)

    # The supplied route governs authz only; the delete spans every route the person wrote under.
    result = await ops.delete_conversation_thread("chat-a", _PERSON_THREAD)

    assert result == {"removed": 2, "route_name": "chat-a", "thread_id": _PERSON_THREAD}
    # One aggregated checkpoint thread, forgotten once.
    assert checkpoint_saver.deleted == [_PERSON_THREAD]
    # Every route index that carried the thread is reclaimed — else a member strands under one.
    for route_name in ("chat-a", "chat-b"):
        assert settings.thread_index_key(route_name, _PERSON_THREAD) not in record_redis._zsets
        assert _PERSON_THREAD not in record_redis._zsets.get(settings.route_threads_key(route_name), {})


async def test_delete_person_thread_404s_when_the_route_is_not_the_persons(
    wired, record_redis, checkpoint_saver, monkeypatch
):
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1",
                linked_at=datetime.now(UTC),
            )
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())

    with pytest.raises(NotFoundError):
        await ops.delete_conversation_thread("chat-b", _PERSON_THREAD)
    assert checkpoint_saver.deleted == []


async def test_delete_person_thread_with_a_turn_in_flight_is_a_409(wired, record_redis, checkpoint_saver, monkeypatch):
    # The in-flight guard reads the aggregated ``bridge:@person:{id}`` key an accepted record
    # carries, so a turn running on any of the person's routes refuses the delete before any
    # teardown — else its completion re-stamps a route index behind the drop.
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
            PersonAddress(
                door="channel",
                routes=["chat-b"],
                channel="whatsapp",
                our_identity="+2",
                address="+2000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person_id == person.person_id else None

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())

    settings = ConversationsSettings()
    record_redis.seed_route("chat-a")
    record_redis.seed_route("chat-b")
    await _seed_accepted(message_id="plive", route_name="chat-a", thread_id=_PERSON_THREAD)

    with pytest.raises(ConflictError, match="turn in flight"):
        await ops.delete_conversation_thread("chat-a", _PERSON_THREAD)
    # Refused before any teardown: the intake record stands, the checkpoint is untouched.
    assert checkpoint_saver.deleted == []
    assert settings.record_key("plive") in record_redis._hashes
    assert settings.thread_index_key("chat-a", _PERSON_THREAD) in record_redis._zsets


async def test_an_interrupted_thread_delete_is_finished_by_a_retry_not_404d(
    wired, record_redis, checkpoint_saver, monkeypatch
):
    # The reclamation runs after the checkpoint delete, so a redis blip mid-drop strands the
    # records/indexes it had not reached. A retry re-runs it and FINISHES, never a 404 that
    # would leave those keys stranded (neither thread index carries a TTL).
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="r0", route_name="chat", thread_id=_THREAD)
    await _seed_record(message_id="r1", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()

    inner_eval = record_redis.eval
    state = {"blown": False}

    async def _eval_blows_once(script, numkeys, *args):
        if "conversations:record:delete" in script and not state["blown"]:
            state["blown"] = True
            raise TimeoutError("redis blip mid-drop")
        return await inner_eval(script, numkeys, *args)

    monkeypatch.setattr(record_redis, "eval", _eval_blows_once)

    with pytest.raises(TimeoutError):
        await ops.delete_conversation_thread("chat", _THREAD)
    # The checkpoint went first, and the thread index survives holding the un-reclaimed work.
    assert checkpoint_saver.deleted == [_THREAD]
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets

    result = await ops.delete_conversation_thread("chat", _THREAD)

    # Not a 404: the retry finished the reclamation.
    assert result["removed"] == 2
    assert result["thread_id"] == _THREAD
    assert settings.thread_index_key("chat", _THREAD) not in record_redis._zsets
    # The checkpoint delete is idempotent under the re-run.
    assert checkpoint_saver.deleted == [_THREAD, _THREAD]


async def test_delete_thread_rejects_a_blank_thread_id(wired):
    with pytest.raises(BadRequestError, match="thread_id"):
        await ops.delete_conversation_thread("chat", "   ")


async def test_delete_thread_rejects_a_bad_route_slug(wired):
    with pytest.raises(BadRequestError, match="route_name"):
        await ops.delete_conversation_thread("Chat Room", _THREAD)


# -- the parked-ask cascade (Phase 1: parked-ask orphaned by thread deletion) --
#
# A conversation thread delete must cascade-cancel every async ``ask_user`` parked on the
# thread, or the deletion orphans the park: its expiry reaper later fires a continuation
# into the now-deleted thread (a delivery retry storm) and its channel correlation stays
# muted until the ~24h deadline. Each delete op wires the shared ``cancel_parks_for_thread``
# entry point; these prove the park's state + its ``pending:expiry`` member + the reverse
# index are all gone after the op.


@pytest.fixture
def interactions_parks(monkeypatch):
    """Wire the ``ask_user`` interactions store the delete-op cascade reaches to a fake
    Redis, and return ``(store, fake)`` so a test can seed a park bound to a thread and
    assert it was cancelled. Mirrors the async-park store harness the interactions suite
    uses (fakeredis via the helper's ``client_ctx`` seam)."""
    from contextlib import asynccontextmanager

    from tai42_skeleton.interactions import helper as interactions_helper
    from tai42_skeleton.interactions.settings import InteractionsSettings
    from tai42_skeleton.interactions.store import InteractionStore

    from .._fakes.interactions_redis import FakeRedis

    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    fake = FakeRedis()

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    settings = InteractionsSettings()
    monkeypatch.setattr(interactions_helper, "client_ctx", _ctx)
    monkeypatch.setattr(interactions_helper, "interactions_settings", lambda: settings)
    return InteractionStore(settings.key_prefix), fake


async def _seed_park(store, fake, *, interaction_id: str, group_id: str, thread_id: str) -> None:
    """Persist one async park bound to ``thread_id`` in the fake interactions store — the
    exact ``add`` the ``ask_user`` async branch performs, carrying the thread id."""
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


async def test_delete_thread_cascade_cancels_a_parked_ask(wired, record_redis, checkpoint_saver, interactions_parks):
    store, fake = interactions_parks
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    await _seed_park(store, fake, interaction_id="i1", group_id="g1", thread_id=_THREAD)
    # A park on an UNRELATED thread must survive the delete untouched.
    other_thread = "bridge:chat:+15559990000"
    await _seed_park(store, fake, interaction_id="keep", group_id="kg", thread_id=other_thread)

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    await _assert_park_cancelled(store, fake, interaction_id="i1", thread_id=_THREAD)
    # The unrelated thread's park is fully intact.
    assert await store.get_state(fake, "keep") is not None
    assert await fake.smembers(store.thread_parks_key(other_thread)) == {"keep"}


async def test_delete_thread_cascade_is_idempotent_on_rerun(wired, record_redis, checkpoint_saver, interactions_parks):
    store, fake = interactions_parks
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_park(store, fake, interaction_id="i1", group_id="g1", thread_id=_THREAD)

    await ops.delete_conversation_thread("chat", _THREAD)
    # A retry of the absolute-forget delete re-runs the cascade cleanly (a no-op second time).
    rerun = await ops.delete_conversation_thread("chat", _THREAD)
    assert rerun == {"removed": 0, "route_name": "chat", "thread_id": _THREAD}
    await _assert_park_cancelled(store, fake, interaction_id="i1", thread_id=_THREAD)


async def test_delete_person_cascade_cancels_a_parked_ask(
    wired, record_redis, checkpoint_saver, interactions_parks, monkeypatch
):
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    store, fake = interactions_parks
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person_id == person.person_id else None

        async def erase(self, p: Person):
            return True, 1

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())
    record_redis.seed_route("chat-a")
    await _seed_record(message_id="pa", route_name="chat-a", thread_id=_PERSON_THREAD)
    await _seed_park(store, fake, interaction_id="i1", group_id="g1", thread_id=_PERSON_THREAD)

    result = await ops.delete_conversation_person("p1")

    assert result == {"person_id": "p1", "removed": 1, "erased": True}
    await _assert_park_cancelled(store, fake, interaction_id="i1", thread_id=_PERSON_THREAD)


async def test_delete_route_cascade_cancels_every_thread_park(wired, record_redis, interactions_parks):
    store, fake = interactions_parks
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    thread_a = "bridge:chat:alice/user-0"
    thread_b = "bridge:chat:alice/user-1"
    await _seed_thread_on("chat", door="api", thread_id=thread_a)
    await _seed_thread_on("chat", door="api", thread_id=thread_b)
    await _seed_park(store, fake, interaction_id="ia", group_id="ga", thread_id=thread_a)
    await _seed_park(store, fake, interaction_id="ib", group_id="gb", thread_id=thread_b)

    assert (await ops.delete_conversation_route("chat"))["removed"] is True

    # Every thread the route owned had its park cancelled.
    await _assert_park_cancelled(store, fake, interaction_id="ia", thread_id=thread_a)
    await _assert_park_cancelled(store, fake, interaction_id="ib", thread_id=thread_b)


# -- the registered target bind validator (D-8: warn-then-error at bind) --------


async def test_create_consults_the_registered_target_validator_and_refuses_on_messages(wired):
    """A plugin's bind validator for the target's kind refuses the create with its message
    lines (a 422), and no row is written — a defect the target carries (a flow reading a
    state no binding supplies) is caught at bind, not deferred to run time."""
    from tai42_skeleton.app import instance

    async def _validator(target_name: str) -> list[str]:
        return [
            f"state acct is read by n1.jq (.acct) but flow {target_name} binds no such state "
            "— bind it on the flow's Bindings tab"
        ]

    instance.app.conversations.register_target_validator("tool", _validator)

    with pytest.raises(ValidationRejected, match="binds no such state"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="tool",
            target_name="echo-tool",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )
    assert "chat" not in wired.rows


async def test_create_passes_when_the_target_validator_returns_no_messages(wired):
    from tai42_skeleton.app import instance

    async def _validator(target_name: str) -> list[str]:
        return []

    instance.app.conversations.register_target_validator("tool", _validator)

    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="tool",
        target_name="echo-tool",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True


async def test_a_tool_validator_does_not_fire_for_an_agent_target(wired):
    """The registry is keyed by target kind: a validator registered for ``tool`` never runs
    for an ``agent`` route."""
    from tai42_skeleton.app import instance

    async def _validator(target_name: str) -> list[str]:
        raise AssertionError("the tool validator must not run for an agent target")

    instance.app.conversations.register_target_validator("tool", _validator)

    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True


async def test_registering_two_validators_for_one_kind_raises(wired):
    from tai42_skeleton.app import instance

    async def _one(target_name: str) -> list[str]:
        return []

    async def _two(target_name: str) -> list[str]:
        return []

    instance.app.conversations.register_target_validator("tool", _one)
    with pytest.raises(ValueError, match="already registered"):
        instance.app.conversations.register_target_validator("tool", _two)
