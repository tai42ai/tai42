"""The quoted message resolves from the thread's own records.

A channel reply names the quoted message by its medium id: an inbound's own id, a sent part's
provider id, or an ask reply's id. An API message names it by the platform's record id and quotes
the answer delivered under it. An asker's quote is looked up in the thread of the ask's send record.
Every miss, another thread's record, a forgotten thread and an expired row are ``not_found``; a
store error propagates. The stores run the real script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from tai42_contract.conversations import AnswerPart, ReplyTo, ReplyToStatus
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_skeleton.conversations import reply_to as reply_to_module
from tai42_skeleton.conversations.models import AskReply, AskSend, ConversationRecord, DeliveryStatus, SentMessage
from tai42_skeleton.conversations.reply_to import resolve_api_reply_to, resolve_ask_reply_to, resolve_reply_to
from tai42_skeleton.conversations.thread_ids import route_thread_id

from .conftest import LuaEnv, _api_route, _channel_route, create_record

_IDENTITY = "+15550001111"
_ADDRESS = "+15550002222"
_THREAD = route_thread_id("line", _ADDRESS)
_IMAGE = MediaItem(kind=MediaKind.IMAGE, url="https://example.com/a.png")


def _record(message_id: str, **update) -> ConversationRecord:
    now = time.time()
    fields = {
        "message_id": message_id,
        "route_name": "line",
        "door": "channel",
        "thread_id": _THREAD,
        "client_address": _ADDRESS,
        "channel": "twilio",
        "our_identity": _IDENTITY,
        "origin": "client",
        "inbound_text": f"ask {message_id}",
        "answer_status": "answered",
        "answer": "the answer",
        "delivery_status": DeliveryStatus.DELIVERED,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(update)
    return ConversationRecord(**fields)


async def _sent(lua_env: LuaEnv, message_id: str, sends: list[SentMessage], **update) -> ConversationRecord:
    """A send record begun and completed through the store, as a send that went out writes it."""
    record = _record(
        message_id,
        origin="send",
        sent_by="ask" if update.get("ask") is not None else "notify",
        inbound_text="",
        delivery_status=DeliveryStatus.SENDING,
        **update,
    )
    route = _channel_route("line")
    await lua_env.store.begin_send_record(record, route=route, settle_deadline=time.time() + 60)
    done = record.model_copy(update={"sends": sends})
    assert (await lua_env.store.complete_send_record(done, route=route)).code == 1
    return done


async def _resolve(lua_env: LuaEnv, medium_message_id: str, *, thread_id: str = _THREAD) -> ReplyTo:
    return await resolve_reply_to(
        lua_env.store,
        channel="twilio",
        our_identity=_IDENTITY,
        client_address=_ADDRESS,
        thread_id=thread_id,
        medium_message_id=medium_message_id,
    )


def _found(message_id: str, text: str) -> ReplyTo:
    return ReplyTo(message_id=message_id, status=ReplyToStatus.FOUND, text=text)


def _status(message_id: str, status: ReplyToStatus) -> ReplyTo:
    return ReplyTo(message_id=message_id, status=status, text=None)


async def _ask_reply(lua_env: LuaEnv, *, sensitive: bool, answer: object, interaction_id: str = "i-1") -> None:
    await lua_env.manager.put_route(_channel_route("line"))
    ask = AskSend(interaction_id=interaction_id, answer_format="text", sensitive=sensitive)
    await _sent(
        lua_env,
        f"s-{interaction_id}",
        [SentMessage(part=0, chars=3, provider_message_ids=[f"q-{interaction_id}"])],
        ask=ask,
        answer="the question",
    )
    reply = AskReply(answer=answer, medium_message_id="r1", answered_at=datetime.now(UTC))
    assert (await lua_env.store.record_ask_reply(interaction_id, reply))[0] == "recorded"


# -- a channel reply ----------------------------------------------------------------------------


async def test_an_inbound_quotes_its_own_text(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    await create_record(lua_env.store, _record("m-in", inbound_text="the first message", inbound_message_id="m1"))

    assert await _resolve(lua_env, "m1") == _found("m1", "the first message")


async def test_a_sent_part_quotes_that_parts_text(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    parts = [AnswerPart(message="part one"), AnswerPart(message="part two")]
    await _sent(
        lua_env,
        "s-parts",
        [
            SentMessage(part=0, chars=8, provider_message_ids=["s1"]),
            SentMessage(part=1, chars=8, provider_message_ids=["s2"]),
        ],
        answer="part one\n\npart two",
        answer_parts=parts,
    )

    assert await _resolve(lua_env, "s2") == _found("s2", "part two")
    assert await _resolve(lua_env, "s1") == _found("s1", "part one")


async def test_a_one_part_answer_quotes_its_answer(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    await _sent(lua_env, "s-one", [SentMessage(part=0, chars=5, provider_message_ids=["s1"])], answer="hello")

    assert await _resolve(lua_env, "s1") == _found("s1", "hello")


async def test_one_medium_message_on_two_records_quotes_both_texts_in_accept_order(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    settings = lua_env.store.settings
    first = _record("m-a", inbound_text="caption", inbound_message_id="m1", provider_message_id="ev-0")
    second = _record("m-b", inbound_text="second file", inbound_message_id="m1", provider_message_id="ev-1")
    await create_record(lua_env.store, first, claim_key=settings.keyspace.dedupe_key("twilio", "ev-0"))
    await create_record(lua_env.store, second, claim_key=settings.keyspace.dedupe_key("twilio", "ev-1"))

    assert await _resolve(lua_env, "m1") == _found("m1", "caption\n\nsecond file")


async def test_an_ask_reply_quotes_its_answer(lua_env):
    await _ask_reply(lua_env, sensitive=False, answer="yes")

    assert await _resolve(lua_env, "r1") == _found("r1", "yes")


async def test_a_form_ask_reply_quotes_its_answer_as_json(lua_env):
    await _ask_reply(lua_env, sensitive=False, answer={"a": 1, "name": "Zoë"})

    assert await _resolve(lua_env, "r1") == _found("r1", '{"a": 1, "name": "Zoë"}')


async def test_a_reply_to_a_sensitive_ask_quotes_as_sensitive_never_the_question(lua_env):
    await _ask_reply(lua_env, sensitive=True, answer="hunter2")

    assert await _resolve(lua_env, "r1") == _status("r1", ReplyToStatus.SENSITIVE)
    assert await _resolve(lua_env, "q-i-1") == _found("q-i-1", "the question")


async def test_a_media_only_sent_part_quotes_as_no_text(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    parts = [AnswerPart(message="", media=[_IMAGE]), AnswerPart(message="words")]
    await _sent(
        lua_env,
        "s-media",
        [
            SentMessage(part=0, chars=0, provider_message_ids=["s1"]),
            SentMessage(part=1, chars=5, provider_message_ids=["s2"]),
        ],
        answer="words",
        answer_parts=parts,
    )

    assert await _resolve(lua_env, "s1") == _status("s1", ReplyToStatus.NO_TEXT)


async def test_a_held_text_wins_over_a_sensitive_reply_under_the_same_id(lua_env):
    await _ask_reply(lua_env, sensitive=True, answer="hunter2")
    await create_record(lua_env.store, _record("m-in", inbound_text="the caption", inbound_message_id="r1"))

    assert await _resolve(lua_env, "r1") == _found("r1", "the caption")


async def test_a_whitespace_only_text_quotes_as_no_text(lua_env):
    await _ask_reply(lua_env, sensitive=False, answer=" \n\t ")

    assert await _resolve(lua_env, "r1") == _status("r1", ReplyToStatus.NO_TEXT)


async def test_an_unknown_id_is_not_found_and_logged_without_address_or_text(lua_env, caplog):
    await lua_env.manager.put_route(_channel_route("line"))
    await create_record(lua_env.store, _record("m-in", inbound_text="secret words", inbound_message_id="m1"))
    caplog.set_level("DEBUG", logger=reply_to_module.__name__)

    assert await _resolve(lua_env, "never-ours") == _status("never-ours", ReplyToStatus.NOT_FOUND)
    assert await _resolve(lua_env, "m1", thread_id="bridge:other:thread") == _status("m1", ReplyToStatus.NOT_FOUND)

    lines = [record.getMessage() for record in caplog.records if record.name == reply_to_module.__name__]
    assert lines
    assert "['m-in'], 1 in another thread" in lines[-1]
    assert all(_ADDRESS not in line and "secret words" not in line for line in lines)


async def test_a_forgotten_thread_and_an_expired_row_are_not_found(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    await create_record(lua_env.store, _record("m-in", inbound_message_id="m1"))
    await create_record(lua_env.store, _record("m-old", inbound_message_id="m2", provider_message_id="ev-2"))
    assert (await _resolve(lua_env, "m1")).status is ReplyToStatus.FOUND

    await lua_env.redis.pexpire(lua_env.store.settings.keyspace.record_key("m-old"), 1)
    await asyncio.sleep(0.01)
    assert await _resolve(lua_env, "m2") == _status("m2", ReplyToStatus.NOT_FOUND)

    await lua_env.store.drop_thread("line", _THREAD)
    assert await _resolve(lua_env, "m1") == _status("m1", ReplyToStatus.NOT_FOUND)


async def test_a_store_error_propagates(lua_env, monkeypatch):
    class _StoreDownError(RuntimeError):
        pass

    async def _down(*args, **kwargs):
        raise _StoreDownError("store down")

    monkeypatch.setattr(lua_env.store, "resolve_message_ref", _down)
    with pytest.raises(_StoreDownError):
        await _resolve(lua_env, "m1")


# -- an API message -----------------------------------------------------------------------------


def _api(message_id: str, **update) -> ConversationRecord:
    thread = route_thread_id("chat", "caller:u-1")
    return _record(
        message_id,
        route_name="chat",
        door="api",
        thread_id=thread,
        client_address="caller:u-1",
        channel=None,
        our_identity=None,
        **update,
    )


async def _api_resolve(lua_env: LuaEnv, message_id: str, *, thread_id: str | None = None) -> ReplyTo:
    thread = thread_id if thread_id is not None else route_thread_id("chat", "caller:u-1")
    return await resolve_api_reply_to(lua_env.store, thread_id=thread, message_id=message_id)


async def test_an_api_reply_quotes_the_answer_never_the_inbound(lua_env):
    await lua_env.manager.put_route(_api_route("chat"))
    await create_record(lua_env.store, _api("m-1", inbound_text="what time", answer="hi there"))

    assert await _api_resolve(lua_env, "m-1") == _found("m-1", "hi there")


@pytest.mark.parametrize(
    "update",
    [
        {"answer_status": None, "answer": None, "delivery_status": DeliveryStatus.ACCEPTED},
        {"answer_status": "silent", "answer": None},
        {"answer_status": None, "answer": None, "delivery_status": DeliveryStatus.SILENT},
        {"answer_status": "merged", "answer": None, "successor_id": "m-2"},
        {"answer_status": "superseded", "answer": None, "successor_id": "m-2"},
        {"answer": "", "answer_parts": [AnswerPart(message="", media=[_IMAGE])]},
    ],
    ids=["running", "silent-outcome", "silent-delivery", "merged", "superseded", "all-media"],
)
async def test_an_api_reply_to_a_record_with_no_delivered_text_is_no_text(lua_env, update):
    await lua_env.manager.put_route(_api_route("chat"))
    running = update.get("delivery_status") is DeliveryStatus.ACCEPTED
    create_kwargs: dict[str, Any] = {"intake_token": "w"} if running else {}
    await create_record(lua_env.store, _api("m-1", **update), **create_kwargs)

    assert await _api_resolve(lua_env, "m-1") == _status("m-1", ReplyToStatus.NO_TEXT)


@pytest.mark.parametrize(
    "update",
    [
        {"answer_status": "error", "answer": "something went wrong"},
        {"delivery_status": DeliveryStatus.FAILED, "answer": "something went wrong"},
        {"origin": "operator", "inbound_text": "", "caller_principal": "op-1", "answer": "something went wrong"},
        {
            "inbound_kind": "event",
            "inbound_text": "",
            "inbound_event": {"id": "e1", "kind": "status", "payload": {}},
            "answer": "something went wrong",
        },
    ],
    ids=["error-outcome", "failed-delivery", "operator-send", "event-turn"],
)
async def test_an_api_reply_quotes_the_text_delivered_under_any_record(lua_env, update):
    await lua_env.manager.put_route(_api_route("chat"))
    await create_record(lua_env.store, _api("m-1", **update))

    assert await _api_resolve(lua_env, "m-1") == _found("m-1", "something went wrong")


async def test_an_api_reply_to_another_thread_or_an_unknown_id_is_not_found(lua_env):
    await lua_env.manager.put_route(_api_route("chat"))
    await create_record(lua_env.store, _api("m-1"))

    other = route_thread_id("chat", "caller:u-2")
    assert await _api_resolve(lua_env, "m-1", thread_id=other) == _status("m-1", ReplyToStatus.NOT_FOUND)
    assert await _api_resolve(lua_env, "m-none") == _status("m-none", ReplyToStatus.NOT_FOUND)


# -- an asker -----------------------------------------------------------------------------------


async def test_an_askers_quote_is_read_in_the_ask_threads_records(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    await create_record(lua_env.store, _record("m-in", inbound_text="the first message", inbound_message_id="m1"))
    ask = AskSend(interaction_id="i-1", answer_format="text", sensitive=False)
    await _sent(lua_env, "s-ask", [SentMessage(part=0, chars=3, provider_message_ids=["q1"])], ask=ask)

    assert await resolve_ask_reply_to("i-1", "m1") == _found("m1", "the first message")


async def test_an_askers_quote_with_no_ask_send_record_is_not_found(lua_env):
    await lua_env.manager.put_route(_channel_route("line"))
    await create_record(lua_env.store, _record("m-in", inbound_message_id="m1"))

    assert await resolve_ask_reply_to("i-unknown", "m1") == _status("m1", ReplyToStatus.NOT_FOUND)


async def test_an_askers_quote_without_a_conversations_store_is_not_found(monkeypatch):
    from types import SimpleNamespace

    from tai42_skeleton.conversations import cache as cache_module

    def _never():
        raise AssertionError("the conversations store is never constructed in memory")

    monkeypatch.setattr(reply_to_module, "conversations_settings", lambda: SimpleNamespace(in_memory=True))
    monkeypatch.setattr(reply_to_module, "get_conversations_manager", _never)
    monkeypatch.setattr(cache_module, "get_conversations_manager", _never)

    assert await resolve_ask_reply_to("i-1", "m1") == _status("m1", ReplyToStatus.NOT_FOUND)
