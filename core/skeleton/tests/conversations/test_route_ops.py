"""The conversation-route CRUD operations: create (pass-role bind + target-exists + jq
compile + callback-secret mint), get/list (secret withheld), delete (with thread-index
reclamation + parked-ask cascade), the slug guard, and the registered target bind validator."""

from __future__ import annotations

import time
from typing import Any, cast

import pytest
from tai42_contract.template import TemplatedText

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, NotFoundError, ValidationRejected

from .conftest import _assert_park_cancelled, _seed_park


class _FakeRouteRequest:
    """The minimal request surface ``_extract_route_create`` reads: an async JSON body and
    the ``route_name`` path param — enough to drive the real HTTP-door extractor without a
    live server."""

    def __init__(self, body: dict, route_name: str) -> None:
        self._body = body
        self.path_params = {"route_name": route_name}

    async def json(self) -> dict:
        return self._body


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

    Once WITHOUT the key in the body (the default path) and once WITH a
    positive override (proving it persists to the stored row)."""
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
    sets it lands the custom participant-facing reply on the stored row, and a body that omits it
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


async def test_route_create_seam_persists_locale(wired):
    """The same extractor -> operation -> store seam carries the route's default ``locale``: a
    body that sets it lands the canonicalized default on the stored row (and the public view),
    and a body that omits it stores ``None`` (no route default)."""
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

    # Default path: body omits the locale; the stored row carries ``None``.
    kwargs = await _extract_route_create(cast(Any, _FakeRouteRequest(dict(base), "chat")))
    result = await op.func(**kwargs)
    assert isinstance(result, dict)
    assert result["created"] is True
    assert wired.rows["chat"].locale is None

    # Default path: a locale rides the body through the seam, is canonicalized, and persists.
    kwargs_locale = await _extract_route_create(cast(Any, _FakeRouteRequest({**base, "locale": "he-il"}, "hebrew")))
    result_locale = await op.func(**kwargs_locale)
    assert isinstance(result_locale, dict)
    assert result_locale["created"] is True
    stored = await wired.get_route("hebrew")
    assert stored is not None
    assert stored.locale == "he-IL"
    assert result_locale["route"]["locale"] == "he-IL"


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


async def test_create_poll_only_api_route_carries_no_secret(wired):
    # A poll-only api route (no callback declared) signs nothing: it mints no secret and
    # reads its answers back from the poll door.
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
    )
    assert result["created"] is True
    assert result["callback_secret"] is None
    assert wired.rows["chat"].callback_secret is None
    assert wired.rows["chat"].callback_url is None


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
        payload_expr=TemplatedText(content="{message: .message, who: .sender}"),
        reply_expr=TemplatedText(content=".reply // null"),
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True
    row = wired.rows["chat"]
    assert row.target_kind == "tool"
    assert row.target_name == "echo-tool"
    assert row.payload_expr == TemplatedText(content="{message: .message, who: .sender}")
    assert row.reply_expr == TemplatedText(content=".reply // null")


async def test_create_tool_route_renders_and_stores_by_id_exprs(wired):
    result = await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="tool",
        target_name="echo-tool",
        payload_expr=TemplatedText(id="route-payload"),
        reply_expr=TemplatedText(id="route-reply"),
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    assert result["created"] is True
    row = wired.rows["chat"]
    # The stored shape holds the templated text by id; the create rendered it (through the
    # bound resource manager) only to compile-check the jq it resolves to.
    assert row.payload_expr == TemplatedText(id="route-payload")
    assert row.reply_expr == TemplatedText(id="route-reply")


async def test_create_rejects_unfetchable_by_id_payload_expr(wired):
    with pytest.raises(BadRequestError, match="payload_expr references stored id 'ghost'"):
        await ops.create_conversation_route(
            route_name="chat",
            door="api",
            target_kind="tool",
            target_name="echo-tool",
            payload_expr=TemplatedText(id="ghost"),
            execution_key="svc",
            callback_url="https://example.com/cb",
        )


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
            payload_expr=TemplatedText(content="{unterminated"),
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
            reply_expr=TemplatedText(content=".["),
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
            reply_expr=TemplatedText(content=".reply"),
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


# -- the registered target bind validator (warn-then-error at bind) --------


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
