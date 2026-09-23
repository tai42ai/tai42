"""Telegram interactive notifications: plain/option/link/section rendering,
callback-data minting, media and location sends, and the chat-action helper.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelNotification,
    LinkOption,
    Option,
    OptionSection,
    ReplyOption,
)
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.channel import TelegramChannel

from .conftest import _TOKEN, _records


async def test_notify_sends_plain_payload_to_default_without_correlation(http_recorder, fake_redis):
    await TelegramChannel().notify(ChannelNotification(message="Deploy finished."))

    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    # Exactly chat_id + text: no reply_markup, no force_reply, no url button.
    assert json.loads(request.content) == {"chat_id": "777", "text": "Deploy finished."}
    # Fire-and-forget: nothing is written to the correlation store.
    assert fake_redis.data == {}
    assert fake_redis.ttls == {}


async def test_notify_caller_recipient_on_allowlist_sends_to_it(http_recorder, fake_redis):
    await TelegramChannel().notify(ChannelNotification(message="ping", recipient="888"))

    assert len(http_recorder.requests) == 1
    assert json.loads(http_recorder.requests[0].content) == {"chat_id": "888", "text": "ping"}
    assert fake_redis.data == {}


async def test_notify_missing_bot_token_raises_naming_var_before_any_other_check(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # The notification also names an unlisted recipient — the token check runs
    # first, so the config error is the one that raises.
    monkeypatch.delenv("CHANNEL_TELEGRAM_BOT_TOKEN")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TELEGRAM_BOT_TOKEN"):
        await TelegramChannel().notify(ChannelNotification(message="ping", recipient="666"))
    assert http_recorder.requests == []


async def test_notify_caller_recipient_not_on_allowlist_refuses_without_sending(http_recorder, fake_redis):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS"):
        await TelegramChannel().notify(ChannelNotification(message="ping", recipient="666"))
    assert http_recorder.requests == []
    assert fake_redis.data == {}


@pytest.mark.parametrize(
    ("responder", "match"),
    [
        pytest.param(
            lambda request: httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 5},
                },
            ),
            "error_code=429",
            id="non-200-json-error",
        ),
        pytest.param(lambda request: httpx.Response(500, text="server error"), "rejected", id="non-200-non-json"),
        pytest.param(lambda request: httpx.Response(200, text="not json"), "non-JSON body", id="non-json"),
        pytest.param(
            lambda request: httpx.Response(200, json={"ok": False, "error_code": 403, "description": "Forbidden"}),
            "error_code=403",
            id="ok-false",
        ),
    ],
)
async def test_notify_failure_response_raises_token_free(http_recorder, fake_redis, responder, match: str):
    http_recorder.responder = responder
    with pytest.raises(ChannelDeliveryError, match=match) as excinfo:
        await TelegramChannel().notify(ChannelNotification(message="ping"))
    assert "notification" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)
    assert fake_redis.data == {}


async def test_notify_returns_sent_message_id(http_recorder, fake_redis):
    # The default responder mints message_id 42; notify returns it as [str].
    result = await TelegramChannel().notify(ChannelNotification(message="Deploy finished."))
    assert result == ["42"]


async def test_notify_ok_without_message_id_raises(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(200, json={"ok": True, "result": {}})
    with pytest.raises(ChannelDeliveryError, match=r"carried no result\.message_id") as excinfo:
        await TelegramChannel().notify(ChannelNotification(message="ping"))
    assert "notification" in str(excinfo.value)


async def test_notify_sender_identity_matches_sends_verbatim_and_returns_id(http_recorder, fake_redis):
    # sender_identity = this bot's numeric id -> send to the given recipient
    # verbatim (allowlist bypassed for the bridge reply), returning the message id.
    result = await TelegramChannel().notify(
        ChannelNotification(message="reply", recipient="555", sender_identity="123456")
    )
    assert result == ["42"]
    assert json.loads(http_recorder.requests[0].content) == {"chat_id": "555", "text": "reply"}


async def test_notify_sender_identity_mismatch_raises_without_sending(http_recorder, fake_redis):
    with pytest.raises(ChannelDeliveryError, match="is not this bot's identity"):
        await TelegramChannel().notify(ChannelNotification(message="reply", recipient="555", sender_identity="999999"))
    assert http_recorder.requests == []


async def test_notify_sender_identity_with_malformed_token_raises(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # Deriving this bot's identity from a token with no numeric prefix is a loud
    # typed error, never a silent send.
    monkeypatch.setenv("CHANNEL_TELEGRAM_BOT_TOKEN", "no-colon-token")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="CHANNEL_TELEGRAM_BOT_TOKEN is malformed"):
        await TelegramChannel().notify(ChannelNotification(message="reply", recipient="555", sender_identity="123456"))
    assert http_recorder.requests == []


def test_channel_advertises_media_and_interactive_notifications():
    # The central notify guard reads these before handing a media / options
    # notification to this channel.
    assert TelegramChannel.supports_media_notifications is True
    assert TelegramChannel.supports_interactive_notifications is True


async def test_notify_sends_body_then_photos_returning_every_id(http_recorder, fake_redis):
    # The body (with link items appended) goes first as one sendMessage, then each
    # image item as its own sendPhoto; every minted message id is returned in order.
    media = [
        MediaItem(kind=MediaKind.LINK, url="https://docs.test/x", caption="doc"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption="pic a"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/b.png", caption=None),
    ]
    result = await TelegramChannel().notify(ChannelNotification(message="See below.", media=media))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendMessage", "sendPhoto", "sendPhoto"]
    body = json.loads(http_recorder.requests[0].content)
    assert body["text"] == "See below.\ndoc: https://docs.test/x"
    assert "reply_markup" not in body  # no options -> no keyboard
    assert json.loads(http_recorder.requests[1].content) == {
        "chat_id": "777",
        "photo": "https://cdn.test/a.png",
        "caption": "pic a",
    }
    # A captionless image sends no caption key.
    assert json.loads(http_recorder.requests[2].content) == {"chat_id": "777", "photo": "https://cdn.test/b.png"}
    assert result == ["42", "42", "42"]
    # Fire-and-forget: no correlation is stored (no options here).
    assert fake_redis.data == {}


async def test_notify_media_only_images_skip_the_sendmessage(http_recorder, fake_redis):
    # A media-only notification (blank message, images only) sends NO text sendMessage — just a
    # sendPhoto per image. Telegram rejects an empty text, so the message is skipped entirely.
    media = [
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption="pic a"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/b.png", caption=None),
    ]
    result = await TelegramChannel().notify(ChannelNotification(message="", media=media))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendPhoto", "sendPhoto"]  # no sendMessage
    assert result == ["42", "42"]


async def test_notify_media_only_with_a_link_renders_the_link_as_the_body(http_recorder, fake_redis):
    # A media-only notification whose media carries a link renders that link AS the sendMessage
    # text (no leading blank line from the empty message), then the image sendPhoto(s).
    media = [
        MediaItem(kind=MediaKind.LINK, url="https://docs.test/x", caption="doc"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption=None),
    ]
    result = await TelegramChannel().notify(ChannelNotification(message="", media=media))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendMessage", "sendPhoto"]
    body = json.loads(http_recorder.requests[0].content)
    assert body["text"] == "doc: https://docs.test/x"
    assert result == ["42", "42"]


async def test_notify_options_render_keyboard_and_store_side_record(http_recorder, fake_redis):
    # Notify reply options render as an inline keyboard on the body message; the option
    # records are kept in a side record (keyed by the anchor message id) so a later tap
    # bridges the option text, with the operator-set notify TTL.
    options: list[Option] = [ReplyOption(text="a"), ReplyOption(text="b"), ReplyOption(text="c")]
    result = await TelegramChannel().notify(ChannelNotification(message="Pick one:", options=options))

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "a", "callback_data": "0"}],
            [{"text": "b", "callback_data": "1"}],
            [{"text": "c", "callback_data": "2"}],
        ]
    }
    assert result == ["42"]
    # No author-set ids: the wire token is the index, id/description null.
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(
        ("0", "a", None, None), ("1", "b", None, None), ("2", "c", None, None)
    )
    assert fake_redis.ttls["channel:telegram:opts:777:42"] == 86_400  # the default option-tap TTL
    # No correlation record — a notify has no callback/ask.
    assert "channel:telegram:corr:777:42" not in fake_redis.data


async def test_notify_reply_option_authored_id_rides_callback_data_and_side_record(http_recorder, fake_redis):
    # An author-set id on a reply option rides verbatim on the wire as callback_data (within
    # Telegram's 64-byte cap) so a tap echoes it back, and is kept in the side record so a
    # bridged tap surfaces it as params.reply_id. A description rides the record too.
    options: list[Option] = [
        ReplyOption(text="Yes", id="yes-1", description="the affirmative"),
        ReplyOption(text="No"),
    ]
    await TelegramChannel().notify(ChannelNotification(message="Pick one:", options=options))

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Yes", "callback_data": "yes-1"}],  # the author-set id, verbatim
            [{"text": "No", "callback_data": "1"}],  # minted index (no author id)
        ]
    }
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(
        ("yes-1", "Yes", "yes-1", "the affirmative"), ("1", "No", None, None)
    )


async def test_notify_link_option_renders_native_url_button_no_record(http_recorder, fake_redis):
    # A link option renders as a native inline url button (a tap opens the url, no message)
    # and keeps no side record; a reply option beside it still gets a callback button + record.
    options: list[Option] = [
        ReplyOption(text="Chat", id="chat"),
        LinkOption(label="Docs", url="https://docs.test/x"),
    ]
    await TelegramChannel().notify(ChannelNotification(message="Pick one:", options=options))

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Chat", "callback_data": "chat"}],
            [{"text": "Docs", "url": "https://docs.test/x"}],
        ]
    }
    # Only the reply option is kept as a record; the link button carries no callback.
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(("chat", "Chat", "chat", None))


async def test_notify_long_authored_id_falls_back_to_minted_index(http_recorder, fake_redis):
    # An author-set id past Telegram's 64-byte callback_data cap cannot ride the wire; the
    # button gets a minted index token, but the record STILL keeps the id so a tap surfaces
    # params.reply_id.
    long_id = "x" * 100
    options: list[Option] = [ReplyOption(text="Yes", id=long_id)]
    await TelegramChannel().notify(ChannelNotification(message="Pick one:", options=options))

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Yes", "callback_data": "0"}]]}
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(("0", "Yes", long_id, None))


async def test_notify_sections_group_rows_and_render_titles_in_text(http_recorder, fake_redis):
    # Telegram has no native sections: the section titles render as text header lines on the
    # body, and the rows across every section render as callback buttons grouped in order.
    sections = [
        OptionSection(title="Fruit", rows=[ReplyOption(text="Apple", id="a"), ReplyOption(text="Pear")]),
        OptionSection(title="Veg", rows=[ReplyOption(text="Kale")]),
    ]
    await TelegramChannel().notify(ChannelNotification(message="Choose:", sections=sections))

    body = json.loads(http_recorder.requests[0].content)
    assert body["text"] == "Choose:\nFruit\nVeg"  # message then section titles
    assert "parse_mode" not in body  # no footer -> plain text
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Apple", "callback_data": "a"}],
            [{"text": "Pear", "callback_data": "1"}],
            [{"text": "Kale", "callback_data": "2"}],
        ]
    }
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(
        ("a", "Apple", "a", None), ("1", "Pear", None, None), ("2", "Kale", None, None)
    )


async def test_notify_footer_renders_as_trailing_italic_line(http_recorder, fake_redis):
    # A footer renders as a trailing muted italic line; a footer needs inline formatting, so
    # the whole body is HTML-escaped and sent with parse_mode=HTML.
    options: list[Option] = [ReplyOption(text="Go")]
    await TelegramChannel().notify(ChannelNotification(message="Ready? <ok>", options=options, footer="expires soon"))

    body = json.loads(http_recorder.requests[0].content)
    assert body["parse_mode"] == "HTML"
    assert body["text"] == "Ready? &lt;ok&gt;\n<i>expires soon</i>"


async def test_notify_header_media_carries_body_as_caption_with_keyboard(http_recorder, fake_redis):
    # A media header rides the standard composition: the media is sent WITH the body as its
    # caption and the keyboard attached, one message, when the body fits the caption cap.
    header = MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/h.png")
    options: list[Option] = [ReplyOption(text="Go", id="go")]
    result = await TelegramChannel().notify(ChannelNotification(message="Look:", options=options, header=header))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendPhoto"]  # one composed message
    body = json.loads(http_recorder.requests[0].content)
    assert body == {
        "chat_id": "777",
        "photo": "https://cdn.test/h.png",
        "caption": "Look:",
        "reply_markup": {"inline_keyboard": [[{"text": "Go", "callback_data": "go"}]]},
    }
    assert result == ["42"]
    # The keyboard-carrying (caption) message anchors the option side record.
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(("go", "Go", "go", None))


def test_mint_callback_data_hash_fallback_on_index_collision():
    # An author-set id numerically equal to another button's index forces the minted
    # token onto the deterministic hash path, keeping every token in the keyboard unique.
    from tai42_channel_telegram.channel import _mint_callback_data

    used = {"1"}  # button 0 authored id "1" already claimed the index-1 token
    token = _mint_callback_data(None, 1, used)
    assert token.startswith("h")
    assert len(token) == 17
    assert token not in used


async def test_notify_header_caption_cap_counts_utf16_units(http_recorder, fake_redis):
    # Telegram caps captions at 1024 UTF-16 code units, not code points: emoji count
    # double. A 600-emoji body (600 code points, 1200 UTF-16 units) must take the
    # SPLIT path (media alone, then text+keyboard), never the caption path.
    header = MediaItem(kind=MediaKind.VIDEO, url="https://cdn.test/h.mp4")
    options: list[Option] = [ReplyOption(text="Go", id="go")]
    body = "\U0001f389" * 600
    await TelegramChannel().notify(ChannelNotification(message=body, options=options, header=header))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendVideo", "sendMessage"]
    assert "caption" not in json.loads(http_recorder.requests[0].content)


async def test_notify_header_media_degrades_to_separate_message_when_body_too_long(http_recorder, fake_redis):
    # A body past Telegram's caption cap cannot ride as a caption: the header media is sent
    # alone, then the text-plus-keyboard message (which anchors the option record).
    header = MediaItem(kind=MediaKind.VIDEO, url="https://cdn.test/h.mp4")
    options: list[Option] = [ReplyOption(text="Go", id="go")]
    long_message = "m" * 1025  # over the 1024-char caption cap
    result = await TelegramChannel().notify(ChannelNotification(message=long_message, options=options, header=header))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendVideo", "sendMessage"]
    header_body = json.loads(http_recorder.requests[0].content)
    assert header_body == {"chat_id": "777", "video": "https://cdn.test/h.mp4"}  # no caption
    text_body = json.loads(http_recorder.requests[1].content)
    assert text_body["text"] == long_message
    assert text_body["reply_markup"] == {"inline_keyboard": [[{"text": "Go", "callback_data": "go"}]]}
    assert result == ["42", "42"]


async def test_notify_document_video_audio_send_by_kind(http_recorder, fake_redis):
    # A document/video/audio media item is sent via its matching Bot API method (caption
    # included when set), not only images.
    media = [
        MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.test/a.pdf", caption="the report", filename="report.pdf"),
        MediaItem(kind=MediaKind.VIDEO, url="https://cdn.test/b.mp4", caption=None),
        MediaItem(kind=MediaKind.AUDIO, url="https://cdn.test/c.mp3", caption="a clip"),
    ]
    result = await TelegramChannel().notify(ChannelNotification(message="Files:", media=media))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendMessage", "sendDocument", "sendVideo", "sendAudio"]
    assert json.loads(http_recorder.requests[1].content) == {
        "chat_id": "777",
        "document": "https://cdn.test/a.pdf",
        "caption": "the report",
    }
    assert json.loads(http_recorder.requests[2].content) == {"chat_id": "777", "video": "https://cdn.test/b.mp4"}
    assert json.loads(http_recorder.requests[3].content) == {
        "chat_id": "777",
        "audio": "https://cdn.test/c.mp3",
        "caption": "a clip",
    }
    assert result == ["42", "42", "42", "42"]


async def test_notify_location_sends_send_location(http_recorder, fake_redis):
    # A bare location (no name+address) is a sendLocation pin.
    location = LocationElement(latitude=51.5, longitude=-0.12)
    result = await TelegramChannel().notify(ChannelNotification(message="Here:", location=location))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendMessage", "sendLocation"]
    assert json.loads(http_recorder.requests[1].content) == {"chat_id": "777", "latitude": 51.5, "longitude": -0.12}
    assert result == ["42", "42"]


async def test_notify_location_with_name_and_address_sends_venue(http_recorder, fake_redis):
    # A location carrying BOTH a name and an address is a sendVenue (Telegram's venue send
    # requires a title AND an address).
    location = LocationElement(latitude=51.5, longitude=-0.12, name="The Office", address="1 High St")
    await TelegramChannel().notify(ChannelNotification(message="", location=location))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendVenue"]  # blank message -> no sendMessage
    assert json.loads(http_recorder.requests[0].content) == {
        "chat_id": "777",
        "latitude": 51.5,
        "longitude": -0.12,
        "title": "The Office",
        "address": "1 High St",
    }


async def test_notify_data_uri_image_is_refused_before_any_send(http_recorder, fake_redis):
    from tai42_contract.channels import ChannelInputError

    media = [MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAA", caption=None)]
    with pytest.raises(ChannelInputError, match="data: image"):
        await TelegramChannel().notify(ChannelNotification(message="hi", media=media))
    assert http_recorder.requests == []


async def test_notify_photo_failure_after_body_names_delivered_ids(http_recorder, fake_redis):
    # A photo that fails mid-send raises naming the ids already delivered (the body),
    # so a partial multi-part send stays visible.
    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/sendPhoto"):
            return httpx.Response(200, json={"ok": False, "error_code": 400, "description": "bad photo"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    http_recorder.responder = responder
    media = [MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption=None)]
    with pytest.raises(ChannelDeliveryError, match=r"after delivering \['42'\]"):
        await TelegramChannel().notify(ChannelNotification(message="hi", media=media))


async def test_send_chat_action_posts_exact_body(http_recorder, fake_redis):
    from tai42_channel_telegram.client import send_chat_action

    await send_chat_action(555, "typing")
    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/sendChatAction"
    assert json.loads(request.content) == {"chat_id": 555, "action": "typing"}


async def test_send_chat_action_ok_false_raises(http_recorder, fake_redis):
    from tai42_channel_telegram.client import send_chat_action

    http_recorder.responder = lambda request: httpx.Response(200, json={"ok": False, "description": "no"})
    with pytest.raises(ChannelDeliveryError, match="sendChatAction rejected"):
        await send_chat_action(555, "typing")


async def test_send_chat_action_unset_token_raises_delivery_error(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # An unset token is a ChannelDeliveryError (not a raw ValueError) so the inbound
    # door's `except ChannelDeliveryError` swallows it and the webhook still acks.
    monkeypatch.delenv("CHANNEL_TELEGRAM_BOT_TOKEN")
    reset_all_settings()
    from tai42_channel_telegram.client import send_chat_action

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_TELEGRAM_BOT_TOKEN"):
        await send_chat_action(555, "typing")
    assert http_recorder.requests == []
