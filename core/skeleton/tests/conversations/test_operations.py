"""The conversation-route CRUD operations: create (pass-role bind + target-exists + jq
compile + callback-secret mint), get/list (secret withheld), delete, and the slug guard."""

from __future__ import annotations

import time

import pytest
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.managers.base_conversations_manager import (
    BaseConversationsManager,
    DoorFlipRefused,
)
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, NotFoundError

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


class _FakeAgents:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    def all_agents(self) -> dict[str, object]:
        return {name: object() for name in self._names}


class _FakeTools:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    async def get_tool(self, key: str) -> object:
        from tai42_skeleton.tools.binding import UnknownToolError

        if key not in self._names:
            raise UnknownToolError(key)
        return object()


class _FakeApp:
    def __init__(self, agents: set[str], tools: set[str]) -> None:
        self.agents = _FakeAgents(agents)
        self.tools = _FakeTools(tools)


@pytest.fixture
def record_redis(monkeypatch) -> FakeRecordRedis:
    """The answer/record store's redis, behind the ops that reach the thread indexes."""
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    fake = FakeRecordRedis()
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    return fake


@pytest.fixture
def wired(monkeypatch, record_redis):
    """Wire a dict-backed manager, a pass-role bind that returns a fingerprint, an agent
    registry holding ``triage`` and a tool registry holding ``echo-tool`` — the standard
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

    monkeypatch.setattr(instance, "app", _FakeApp({"triage"}, {"echo-tool"}), raising=False)
    return manager


async def test_create_api_route_mints_and_shows_the_secret_once(wired):
    result = await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True
    assert result["callback_secret"]  # shown once here
    # The stored fingerprint is the one the bind derived, never a client value.
    assert wired.rows["support"].execution_key_fingerprint == "fp-derived"
    assert wired.rows["support"].callback_secret == result["callback_secret"]
    # The route view withholds the secret.
    assert "callback_secret" not in result["route"]


async def test_create_channel_route_carries_no_secret(wired):
    result = await ops.create_conversation_route(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
    )
    assert result["callback_secret"] is None
    assert wired.rows["line"].callback_secret is None


async def test_create_is_an_upsert(wired):
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    result = await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc2",
        callback_url="https://example.com/cb2",
    )
    assert result["created"] is False
    assert wired.rows["support"].execution_key == "svc2"


async def test_a_channel_identity_is_stored_canonicalized(wired):
    # Inbound routing matches by equality on the canonical form, so the row is stored
    # canonicalized — a verbatim row would match nothing and hide duplicates.
    await ops.create_conversation_route(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="triage",
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
        target_name="triage",
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
            target_name="triage",
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
            target_name="triage",
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
            target_name="triage",
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
            target_name="triage",
            execution_key="svc",
            channel="twi:lio",
            our_identity="+15550001111",
        )
    assert wired.rows == {}


async def test_create_rejects_unknown_agent(wired):
    with pytest.raises(NotFoundError, match="agent not found"):
        await ops.create_conversation_route(
            route_name="support",
            door="api",
            target_kind="agent",
            target_name="ghost",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_tool_route_validates_tool_and_compiles_exprs(wired):
    result = await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="tool",
        target_name="echo-tool",
        payload_expr="{message: .message, who: .sender}",
        reply_expr=".reply // null",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True
    row = wired.rows["support"]
    assert row.target_kind == "tool"
    assert row.target_name == "echo-tool"
    assert row.payload_expr == "{message: .message, who: .sender}"
    assert row.reply_expr == ".reply // null"


async def test_create_rejects_unknown_tool(wired):
    with pytest.raises(NotFoundError, match="tool not found"):
        await ops.create_conversation_route(
            route_name="support",
            door="api",
            target_kind="tool",
            target_name="ghost-tool",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_rejects_invalid_payload_expr(wired):
    with pytest.raises(BadRequestError, match="invalid payload_expr"):
        await ops.create_conversation_route(
            route_name="support",
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
            route_name="support",
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
            route_name="support",
            door="api",
            target_kind="agent",
            target_name="triage",
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
            target_name="triage",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


async def test_create_bind_refusal_leaves_no_row(wired, monkeypatch):
    async def _refuse(caller, execution_key):
        raise BadRequestError("not yours")

    monkeypatch.setattr(ops, "assert_execution_key_bindable", _refuse)
    with pytest.raises(BadRequestError, match="not yours"):
        await ops.create_conversation_route(
            route_name="support",
            door="api",
            target_kind="agent",
            target_name="triage",
            execution_key="svc",
            callback_url="https://example.com/cb",
        )
    assert "support" not in wired.rows


async def test_get_withholds_the_secret_and_404s_unknown(wired):
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    view = await ops.get_conversation_route("support")
    assert "callback_secret" not in view
    assert view["route_name"] == "support"
    with pytest.raises(NotFoundError):
        await ops.get_conversation_route("missing")


async def test_get_rejects_a_colon_route_name(wired):
    with pytest.raises(BadRequestError, match="slug"):
        await ops.get_conversation_route("bad:name")


async def test_list_withholds_secrets(wired):
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    listed = await ops.list_conversation_routes()
    assert listed["total"] == 1
    assert all("callback_secret" not in item for item in listed["items"])


async def test_delete_removes_then_404s(wired):
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert (await ops.delete_conversation_route("support"))["removed"] is True
    with pytest.raises(NotFoundError):
        await ops.delete_conversation_route("support")


async def test_delete_reclaims_the_routes_thread_indexes(wired, record_redis):
    # Neither thread index carries a TTL and the prune pass only walks LIVE routes, so a
    # delete that left them behind stranded them in redis forever.
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    settings = ConversationsSettings()
    now = time.time()
    for index in range(2):
        await ConversationRecordStore(settings).create_record(
            ConversationRecord(
                message_id=f"m{index}",
                route_name="support",
                door="api",
                thread_id=f"bridge:support:alice/user-{index}",
                client_address=f"alice/user-{index}",
                caller_principal="alice",
                callback_url="https://example.com/cb",
                inbound_text="ask",
                answer_status="answered",
                answer="the answer",
                delivery_status=DeliveryStatus.DELIVERED,
                created_at=now,
                updated_at=now,
            )
        )
    assert settings.route_threads_key("support") in record_redis._zsets

    assert (await ops.delete_conversation_route("support"))["removed"] is True

    assert settings.route_threads_key("support") not in record_redis._zsets
    for index in range(2):
        assert settings.thread_index_key("support", f"bridge:support:alice/user-{index}") not in record_redis._zsets


async def test_an_interrupted_delete_is_finished_by_a_retry_instead_of_404ing(wired, record_redis, monkeypatch):
    # The reclamation runs AFTER the routing row is gone, so a socket timeout, a SIGTERM or
    # a redis blip mid-loop strands whatever it had not reached: nothing walks a name that
    # no longer routes. A retry that answered 404 would leave those keys unnameable forever.
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_thread_on("support", door="api", thread_id="bridge:support:alice/user-1")
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
        await ops.delete_conversation_route("support")

    # The routing row is already gone, so no message can open a thread on the name...
    assert "support" not in wired.rows
    # ...and the route's thread index survives, holding the work the run never reached.
    assert settings.route_threads_key("support") in record_redis._zsets

    result = await ops.delete_conversation_route("support")

    # Not a 404: the retry re-ran the reclamation and says the row was not this call's to
    # remove.
    assert result == {"removed": False, "route_name": "support"}
    assert settings.route_threads_key("support") not in record_redis._zsets
    assert settings.thread_index_key("support", "bridge:support:alice/user-1") not in record_redis._zsets


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
            inbound_text="ask",
            answer_status="answered",
            answer="the answer",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


async def test_flipping_the_door_of_a_route_that_holds_threads_is_refused(wired, record_redis):
    # The doors key their threads differently and the read doors authorize from that shape,
    # so an api→channel flip 404s the owner out of a transcript they own while the
    # single-message door still hands them the same records.
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_thread_on("support", door="api", thread_id="bridge:support:alice/user-1")

    with pytest.raises(BadRequestError, match="holds 1 thread"):
        await ops.create_conversation_route(
            route_name="support",
            door="channel",
            target_kind="agent",
            target_name="triage",
            execution_key="svc",
            channel="twilio",
            our_identity="+15550001111",
        )
    # Refused BEFORE any write: the row still routes exactly as it did.
    assert wired.rows["support"].door == "api"


async def test_a_thread_opened_during_the_edit_still_refuses_the_door_flip(wired, record_redis, monkeypatch):
    # A count read a few awaits before the write is no guard at all: the edit resolves its
    # target, compiles its exprs and binds its execution key in between, and a first message
    # landing in that window opens the very thread the refusal exists to protect — after
    # which the flip lands anyway, and the revert is refused too.
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )

    async def _bind_while_a_first_message_lands(caller, execution_key):
        await _seed_thread_on("support", door="api", thread_id="bridge:support:alice/user-1")
        return "fp-derived"

    monkeypatch.setattr(ops, "assert_execution_key_bindable", _bind_while_a_first_message_lands)

    with pytest.raises(BadRequestError, match="holds 1 thread"):
        await ops.create_conversation_route(
            route_name="support",
            door="channel",
            target_kind="agent",
            target_name="triage",
            execution_key="svc",
            channel="twilio",
            our_identity="+15550001111",
        )
    assert wired.rows["support"].door == "api"


async def test_the_door_of_a_route_holding_no_thread_is_still_editable(wired, record_redis):
    # The refusal is about orphaning threads, not about the door being immutable.
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    result = await ops.create_conversation_route(
        route_name="support",
        door="channel",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
    )
    assert result["created"] is False
    assert wired.rows["support"].door == "channel"


async def test_an_edit_that_keeps_the_door_is_untouched_by_the_guard(wired, record_redis):
    await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_thread_on("support", door="api", thread_id="bridge:support:alice/user-1")

    result = await ops.create_conversation_route(
        route_name="support",
        door="api",
        target_kind="agent",
        target_name="triage",
        execution_key="svc2",
        callback_url="https://example.com/cb2",
    )
    assert result["created"] is False
    assert wired.rows["support"].execution_key == "svc2"


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
        await ops.list_conversation_threads("support")
    with pytest.raises(NotSupportedError):
        await ops.get_conversation_thread("support", "bridge:support:+15550001111")
