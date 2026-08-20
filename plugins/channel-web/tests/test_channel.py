"""``WebChannel.deliver`` and ``.notify`` — recipient splitting, reserve-before-
append ordering, the release-on-append-failure path, and the text-only notify."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_web import channel as channel_module
from tai42_channel_web.channel import WebChannel

from .conftest import CALLBACK, IDENTITY, VISITOR_ID, FakeRedis, make_delivery, make_notification

pytestmark = pytest.mark.usefixtures("web_env")

_TRANSCRIPT_KEY = f"channel:web:transcript:{IDENTITY}:{VISITOR_ID}"
_QUESTION_KEY = "channel:web:question:int-1"


def _only_entry(fake: FakeRedis) -> dict:
    entries = fake.streams[_TRANSCRIPT_KEY]
    assert len(entries) == 1
    return json.loads(entries[0][1]["data"])


async def test_deliver_reserves_record_and_appends_question(fake_redis: FakeRedis):
    await WebChannel().deliver(make_delivery())

    record = json.loads(fake_redis.store[_QUESTION_KEY])
    assert record == {
        "callback_url": CALLBACK,
        "identity": IDENTITY,
        "address": VISITOR_ID,
        "timeout_at": record["timeout_at"],
        "restores": 0,
    }
    payload = _only_entry(fake_redis)
    assert payload["interaction_id"] == "int-1"
    assert payload["question"] == "Deploy to prod?"


async def test_deliver_reserves_before_appending(fake_redis: FakeRedis):
    await WebChannel().deliver(make_delivery())
    kinds = [name for name, _ in fake_redis.events]
    assert kinds.index("redis_set") < kinds.index("redis_xadd")


@pytest.mark.parametrize(
    ("answer_format", "options"),
    [("text", None), ("confirm", None), ("external", None), ("select", ["a", "b"])],
)
async def test_deliver_accepts_all_four_formats(fake_redis: FakeRedis, answer_format: str, options: list[str] | None):
    await WebChannel().deliver(make_delivery(answer_format=answer_format, options=options))
    payload = _only_entry(fake_redis)
    assert payload["answer_format"] == answer_format
    assert payload["options"] == options


@pytest.mark.parametrize("answer_format", ["text", "confirm", "select"])
async def test_deliver_keeps_the_callback_ticket_off_the_transcript(fake_redis: FakeRedis, answer_format: str):
    # The frame is persisted for the transcript TTL and shipped to the browser; the
    # callback url is a bearer ticket, and only the external widget needs to open it.
    options = ["a", "b"] if answer_format == "select" else None
    await WebChannel().deliver(make_delivery(answer_format=answer_format, options=options))
    assert "callback_url" not in _only_entry(fake_redis)
    # It is still recorded server-side, where the answer door forwards from.
    assert json.loads(fake_redis.store[_QUESTION_KEY])["callback_url"] == CALLBACK


async def test_deliver_ships_the_callback_ticket_to_the_external_widget(fake_redis: FakeRedis):
    await WebChannel().deliver(make_delivery(answer_format="external"))
    assert _only_entry(fake_redis)["callback_url"] == CALLBACK


_FORM_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


def test_web_channel_advertises_form_delivery():
    # The plain class attribute the ask_user helper reads with ``getattr`` before it
    # routes a form delivery here; absent it, this channel would never receive one.
    assert WebChannel.supports_form_delivery is True


async def test_deliver_ships_the_form_schema_to_the_widget(fake_redis: FakeRedis):
    await WebChannel().deliver(make_delivery(answer_format="form", options=None, schema=_FORM_SCHEMA))
    payload = _only_entry(fake_redis)
    assert payload["answer_format"] == "form"
    assert payload["schema"] == _FORM_SCHEMA


@pytest.mark.parametrize("answer_format", ["text", "confirm", "select", "external"])
async def test_deliver_keeps_the_schema_off_a_non_form_widget(fake_redis: FakeRedis, answer_format: str):
    # The schema is carried only for the form widget that renders it; every other
    # format has none, so the key is absent from the frame.
    options = ["a", "b"] if answer_format == "select" else None
    await WebChannel().deliver(make_delivery(answer_format=answer_format, options=options))
    assert "schema" not in _only_entry(fake_redis)


async def test_deliver_refuses_an_identity_the_doors_could_never_read_back(fake_redis: FakeRedis):
    # The split takes the LAST colon, so this leaves the identity ``odd:name`` — which
    # the stream door refuses, so the visitor could never open that transcript and the
    # ask would black-hole. It is refused here instead, loudly, before any write.
    with pytest.raises(ChannelDeliveryError, match="not a usable web route identity"):
        await WebChannel().deliver(make_delivery(recipient=f"odd:name:{VISITOR_ID}"))
    assert not fake_redis.store
    assert not fake_redis.streams


async def test_deliver_refuses_an_over_long_identity(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="not a usable web route identity"):
        await WebChannel().deliver(make_delivery(recipient=f"{'a' * 257}:{VISITOR_ID}"))
    assert not fake_redis.streams


async def test_notify_refuses_an_identity_the_doors_could_never_read_back(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="not a usable web route identity"):
        await WebChannel().notify(make_notification(sender_identity=f"{IDENTITY}:extra"))
    assert not fake_redis.streams


async def test_deliver_trims_the_identity_to_the_bridge_canonical_form(fake_redis: FakeRedis):
    # The bridge trims every address, so an untrimmed identity here would key a
    # transcript nothing else ever writes to.
    await WebChannel().deliver(make_delivery(recipient=f"  {IDENTITY}  :{VISITOR_ID}"))
    assert _TRANSCRIPT_KEY in fake_redis.streams


async def test_deliver_rejects_a_blank_identity(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="not a usable web route identity"):
        await WebChannel().deliver(make_delivery(recipient=f"   :{VISITOR_ID}"))


async def test_notify_trims_the_bridge_sender_identity(fake_redis: FakeRedis):
    await WebChannel().notify(make_notification(sender_identity=f" {IDENTITY} "))
    assert _TRANSCRIPT_KEY in fake_redis.streams


async def test_deliver_refuses_expired_deadline_before_any_write(fake_redis: FakeRedis):
    delivery = make_delivery(timeout_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await WebChannel().deliver(delivery)
    assert not fake_redis.store
    assert not fake_redis.streams


async def test_deliver_requires_recipient(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="no recipient requested"):
        await WebChannel().deliver(make_delivery(recipient=None))


async def test_deliver_rejects_recipient_without_identity_and_address(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="must be '<identity>:<visitor-id>'"):
        await WebChannel().deliver(make_delivery(recipient="usr-only"))


async def test_deliver_releases_its_reservation_under_repeated_cancellation(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # ``CancelledError`` is a BaseException, so an ``except Exception`` unwind misses
    # it and the reservation would sit pinned until its TTL — the visitor sees no
    # question and the id cannot be reserved again.
    appending = asyncio.Event()
    real_delete = fake_redis.delete

    async def _hang(*args: object, **kwargs: object) -> str:
        appending.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover - cancelled above

    async def _round_trip_delete(key: str) -> int:
        # A real DEL suspends on the socket; a plain ``await release_question`` in the
        # unwind is cancelled exactly here, which is what the shield exists for.
        await asyncio.sleep(0)
        return await real_delete(key)

    monkeypatch.setattr(fake_redis, "xadd", _hang)
    monkeypatch.setattr(fake_redis, "delete", _round_trip_delete)
    task = asyncio.create_task(WebChannel().deliver(make_delivery()))
    await appending.wait()

    # A caller that keeps cancelling — a shutdown escalating, a supervisor retrying —
    # delivers a ``CancelledError`` at EVERY await of the unwind. One ``.cancel()``
    # delivers exactly one, which an unshielded release survives by accident.
    while not task.done():
        task.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shielded release runs on its own task, so let it finish.
    while channel_module._pending_releases:
        await asyncio.sleep(0)
    assert _QUESTION_KEY not in fake_redis.store


async def test_a_cancellation_after_the_append_keeps_the_reservation(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # Leaving the write-order gate is a suspension point. A cancellation delivered
    # there, with the ``appended`` flag set only AFTER the ``async with``, releases
    # the reservation for a question that is already in the visitor's transcript:
    # they see the ask, and their answer 404s.
    @asynccontextmanager
    async def _cancelled_on_exit(identity: str, address: str) -> AsyncIterator[None]:
        yield
        raise asyncio.CancelledError

    monkeypatch.setattr(channel_module, "transcript_order", _cancelled_on_exit)
    with pytest.raises(asyncio.CancelledError):
        await WebChannel().deliver(make_delivery())

    assert _TRANSCRIPT_KEY in fake_redis.streams
    assert _QUESTION_KEY in fake_redis.store


async def test_a_failing_release_never_supersedes_the_error_that_caused_it(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # The release runs in a ``finally``; raising from there would replace the real
    # failure with a second one and send the caller looking at the wrong thing.
    async def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("xadd down")

    async def _release_boom(key: str) -> int:
        raise RuntimeError("del down")

    monkeypatch.setattr(fake_redis, "xadd", _boom)
    monkeypatch.setattr(fake_redis, "delete", _release_boom)
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="xadd down"):
        await WebChannel().deliver(make_delivery())

    assert any("could not release the reservation" in r.getMessage() for r in caplog.records)


async def test_deliver_releases_reservation_when_append_fails(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("xadd down")

    monkeypatch.setattr(fake_redis, "xadd", _boom)
    with pytest.raises(RuntimeError, match="xadd down"):
        await WebChannel().deliver(make_delivery())
    # The reservation is freed rather than held until its TTL.
    assert _QUESTION_KEY not in fake_redis.store


async def test_notify_appends_out_message_and_returns_id(fake_redis: FakeRedis):
    ids = await WebChannel().notify(make_notification(message="Working on it."))
    assert len(ids) == 1
    payload = json.loads(fake_redis.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload["direction"] == "out"
    assert payload["text"] == "Working on it."
    assert payload["id"] == ids[0]


async def test_notify_without_sender_identity_splits_composite_recipient(fake_redis: FakeRedis):
    # The ``notify_user`` door sets no sender_identity, so the identity rides in the
    # recipient — the entry must still land on the same transcript.
    ids = await WebChannel().notify(
        make_notification(sender_identity=None, recipient=f"{IDENTITY}:{VISITOR_ID}", message="No bridge.")
    )
    payload = json.loads(fake_redis.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload["text"] == "No bridge."
    assert payload["id"] == ids[0]


async def test_notify_without_sender_identity_rejects_bare_recipient(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="must be '<identity>:<visitor-id>'"):
        await WebChannel().notify(make_notification(sender_identity=None, recipient=VISITOR_ID))


async def test_notify_requires_recipient(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="no recipient requested"):
        await WebChannel().notify(make_notification(recipient=None))


async def test_notify_without_sender_identity_requires_recipient(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="no recipient requested"):
        await WebChannel().notify(make_notification(sender_identity=None, recipient=None))


def _only_media_entry(fake: FakeRedis) -> dict:
    entries = fake.streams[_TRANSCRIPT_KEY]
    assert len(entries) == 1
    assert entries[0][1]["event"] == "chat.media"
    return json.loads(entries[0][1]["data"])


async def test_notify_media_appends_one_media_card(fake_redis: FakeRedis):
    # A media notification lands as ONE chat.media card carrying the message text, the
    # image items, and its minted id — no separate chat.message entry.
    ids = await WebChannel().notify(
        ChannelNotification(
            message="see this",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/p.png", caption="a pattern")],
        )
    )
    payload = _only_media_entry(fake_redis)
    assert payload["id"] == ids[0]
    assert payload["direction"] == "out"
    assert payload["text"] == "see this"
    assert payload["media"] == [{"kind": "image", "url": "https://cdn.example/p.png", "caption": "a pattern"}]
    assert "options" not in payload


async def test_notify_links_only_rides_the_media_array_as_a_card_attachment(fake_redis: FakeRedis):
    # A link item rides the frame's media array as a card link element (the page renders
    # it as a safe anchor); the body is the message unchanged — no appended link lines.
    # A links-only notification therefore still lands as a chat.media frame WITH media
    # present, so the widget's at-least-one-of media/options contract holds.
    await WebChannel().notify(
        ChannelNotification(
            message="see this",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            media=[MediaItem(kind=MediaKind.LINK, url="https://example.com/a", caption="Item A")],
        )
    )
    payload = _only_media_entry(fake_redis)
    assert payload["text"] == "see this"
    assert payload["media"] == [{"kind": "link", "url": "https://example.com/a", "caption": "Item A"}]


async def test_notify_mixed_media_rides_the_array_in_order(fake_redis: FakeRedis):
    # Every media item rides the frame's array in the order it was given — an image as
    # an inline picture, a link as a card link element.
    await WebChannel().notify(
        ChannelNotification(
            message="see this",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            media=[
                MediaItem(kind=MediaKind.LINK, url="https://example.com/a", caption="Item A"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/p.png"),
            ],
        )
    )
    payload = _only_media_entry(fake_redis)
    assert payload["media"] == [
        {"kind": "link", "url": "https://example.com/a", "caption": "Item A"},
        {"kind": "image", "url": "https://cdn.example/p.png"},
    ]


async def test_notify_refuses_a_data_image(fake_redis: FakeRedis):
    # The page renders an image only from an absolute https source; a data: URI is
    # refused loudly and nothing is written.
    notification = ChannelNotification(
        message="see this",
        recipient=VISITOR_ID,
        sender_identity=IDENTITY,
        media=[MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAA")],
    )
    with pytest.raises(ChannelInputError, match="data: image"):
        await WebChannel().notify(notification)
    assert _TRANSCRIPT_KEY not in fake_redis.streams


async def test_notify_options_appends_a_card_with_options(fake_redis: FakeRedis):
    # A text-only-with-options notification also lands as ONE chat.media card.
    ids = await WebChannel().notify(
        ChannelNotification(
            message="pick one",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            options=["Item A", "Item B"],
        )
    )
    payload = _only_media_entry(fake_redis)
    assert payload["id"] == ids[0]
    assert payload["text"] == "pick one"
    assert payload["options"] == ["Item A", "Item B"]
    assert "media" not in payload


async def test_notify_options_and_media_appends_a_card_with_both(fake_redis: FakeRedis):
    await WebChannel().notify(
        ChannelNotification(
            message="a card with a list",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/p.png")],
            options=["Item A"],
        )
    )
    payload = _only_media_entry(fake_redis)
    assert payload["media"] == [{"kind": "image", "url": "https://cdn.example/p.png"}]
    assert payload["options"] == ["Item A"]


async def test_notify_options_and_template_rejected_at_contract(fake_redis: FakeRedis):
    # The contract refuses options+template BEFORE the channel is reached.
    with pytest.raises(ValidationError, match="mutually exclusive"):
        ChannelNotification(
            message="x",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            options=["Item A"],
            template=ChannelTemplate(name="welcome", language="en"),
        )


async def test_notify_refuses_template(fake_redis: FakeRedis):
    notification = ChannelNotification(
        message="hello",
        recipient=VISITOR_ID,
        sender_identity=IDENTITY,
        template=ChannelTemplate(name="welcome", language="en"),
    )
    with pytest.raises(NotImplementedError, match="no vendor templates"):
        await WebChannel().notify(notification)
