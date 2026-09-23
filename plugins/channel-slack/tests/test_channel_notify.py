"""Slack interactive notifications: reply/link/section rendering, header/footer and
media composition, sender-identity handling, and token/recipient guards.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
    LinkOption,
    Option,
    OptionSection,
    ReplyOption,
)
from tai42_contract.interactions.models import (
    LocationElement,
    MediaItem,
    MediaKind,
)
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.blocks import (
    LINK_ACTION_PREFIX,
    REPLY_ACTION_PREFIX,
    decode_reply_value,
)
from tai42_channel_slack.channel import SlackChannel

from .conftest import (
    TEST_ALLOWED_RECIPIENT,
    TEST_BOT_TOKEN,
    TEST_BOT_USER_ID,
    TEST_DEFAULT_RECIPIENT,
    _ok_response,
    make_delivery,
)

pytestmark = pytest.mark.usefixtures("slack_env")


async def test_notify_sends_plain_payload_returns_ts_and_writes_nothing(http_script, fake_redis):
    # The text is the bare message (no deadline, no reply-in-thread instruction,
    # no callback link); notify returns the posted ts and stores no correlation.
    http_script.results.append(_ok_response(ts="1712345678.000100"))

    result = await SlackChannel().notify(ChannelNotification(message="Deploy finished."))

    assert result == ["1712345678.000100"]
    (request,) = http_script.requests
    assert str(request.url) == "https://slack.com/api/chat.postMessage"
    assert request.headers["Authorization"] == f"Bearer {TEST_BOT_TOKEN}"
    payload = json.loads(request.content)
    assert payload == {"channel": TEST_DEFAULT_RECIPIENT, "text": "Deploy finished."}
    assert fake_redis.store == {}
    assert fake_redis.ttls == {}


async def test_notify_reply_options_render_buttons_with_text_fallback(http_script, fake_redis):
    # Typed reply options render as an actions block of buttons: each button's value is a
    # JSON envelope carrying the submit text (no author id here), and the text fallback lists
    # them as suggestion lines so the notification preview shows them too.
    http_script.results.append(_ok_response(ts="1.1"))
    options: list[Option] = [ReplyOption(text="a"), ReplyOption(text="b")]
    result = await SlackChannel().notify(ChannelNotification(message="Pick one:", options=options))

    assert result == ["1.1"]
    payload = json.loads(http_script.requests[0].content)
    actions = payload["blocks"][-1]
    assert actions["type"] == "actions"
    assert [e["action_id"] for e in actions["elements"]] == [
        f"{REPLY_ACTION_PREFIX}0",
        f"{REPLY_ACTION_PREFIX}1",
    ]
    # Each value decodes back to its submit text with no id (none was authored).
    assert [decode_reply_value(e["value"]) for e in actions["elements"]] == [("a", None), ("b", None)]
    assert [e["text"]["text"] for e in actions["elements"]] == ["a", "b"]
    assert payload["text"] == "Pick one:\n• a\n• b"


async def test_notify_reply_option_carries_authored_id_in_value_envelope(http_script, fake_redis):
    # An author-set option id rides the button value verbatim so a tap can echo it back as
    # params.reply_id; the display label stays the option text.
    http_script.results.append(_ok_response(ts="1.1"))
    options: list[Option] = [ReplyOption(text="Yes please", id="opt-yes"), ReplyOption(text="No thanks", id="opt-no")]
    await SlackChannel().notify(ChannelNotification(message="Confirm?", options=options))

    actions = json.loads(http_script.requests[0].content)["blocks"][-1]
    assert [decode_reply_value(e["value"]) for e in actions["elements"]] == [
        ("Yes please", "opt-yes"),
        ("No thanks", "opt-no"),
    ]


async def test_notify_reply_option_description_folds_into_context_block(http_script, fake_redis):
    # Slack buttons carry no description, so a reply option's description is folded into a
    # preceding muted context block rather than dropped.
    http_script.results.append(_ok_response(ts="1.1"))
    options: list[Option] = [
        ReplyOption(text="Retry", description="Run the step again"),
        ReplyOption(text="Replace"),
    ]
    await SlackChannel().notify(ChannelNotification(message="Choose:", options=options))

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    context = next(b for b in blocks if b["type"] == "context")
    assert context["elements"] == [{"type": "mrkdwn", "text": "*Retry* — Run the step again"}]
    # The context precedes the actions block of buttons.
    assert blocks.index(context) < blocks.index(next(b for b in blocks if b["type"] == "actions"))


async def test_notify_link_option_renders_url_button(http_script, fake_redis):
    # A link option renders as a url button (tap opens the url, submits nothing) — its
    # action_id marks it a link tap the interactivity door acks and ignores.
    http_script.results.append(_ok_response(ts="1.1"))
    options: list[Option] = [LinkOption(label="Open dashboard", url="https://app.test/dash")]
    await SlackChannel().notify(ChannelNotification(message="Here:", options=options))

    actions = json.loads(http_script.requests[0].content)["blocks"][-1]
    (button,) = actions["elements"]
    assert button == {
        "type": "button",
        "action_id": f"{LINK_ACTION_PREFIX}0",
        "text": {"type": "plain_text", "text": "Open dashboard"},
        "url": "https://app.test/dash",
    }


async def test_notify_sections_render_titled_button_groups(http_script, fake_redis):
    # A sectioned option list renders each section as a titled mrkdwn section followed by its
    # reply rows as buttons; action_ids stay unique across sections.
    http_script.results.append(_ok_response(ts="1.1"))
    sections = [
        OptionSection(title="Fruit", rows=[ReplyOption(text="Apple"), ReplyOption(text="Pear")]),
        OptionSection(title="Veg", rows=[ReplyOption(text="Carrot")]),
    ]
    await SlackChannel().notify(ChannelNotification(message="Pick:", sections=sections))

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    titles = [b["text"]["text"] for b in blocks if b["type"] == "section" and b["text"]["type"] == "mrkdwn"]
    assert titles == ["*Fruit*", "*Veg*"]
    action_ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert action_ids == [f"{REPLY_ACTION_PREFIX}0", f"{REPLY_ACTION_PREFIX}1", f"{REPLY_ACTION_PREFIX}2"]
    assert json.loads(http_script.requests[0].content)["text"] == "Pick:\nFruit:\n• Apple\n• Pear\nVeg:\n• Carrot"


async def test_notify_header_and_footer_compose_the_interactive_message(http_script, fake_redis):
    # A header image rides ABOVE the message body; a footer renders as a muted context block
    # at the end. Both require an interactive surface (contract), so options are present.
    http_script.results.append(_ok_response(ts="1.1"))
    notification = ChannelNotification(
        message="Menu",
        options=[ReplyOption(text="Start")],
        header=MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/banner.png", caption="banner"),
        footer="Powered by tai42",
    )
    await SlackChannel().notify(notification)

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    assert blocks[0] == {"type": "image", "image_url": "https://cdn.test/banner.png", "alt_text": "banner"}
    assert blocks[1] == {"type": "section", "text": {"type": "plain_text", "text": "Menu"}}
    assert blocks[-1] == {"type": "context", "elements": [{"type": "mrkdwn", "text": "Powered by tai42"}]}


async def test_notify_header_non_image_degrades_to_link_line(http_script, fake_redis):
    # A non-image header (Slack cannot inline a file without an upload seam) degrades to a
    # labelled link line, above the body.
    http_script.results.append(_ok_response(ts="1.1"))
    notification = ChannelNotification(
        message="Report ready",
        options=[ReplyOption(text="Ack")],
        header=MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.test/q3.pdf", caption="Q3", filename="q3.pdf"),
    )
    await SlackChannel().notify(notification)

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "<https://cdn.test/q3.pdf|Q3> (q3.pdf)"}}


async def test_notify_file_media_degrades_to_labelled_link_lines(http_script, fake_redis):
    # document/video/audio media cannot inline on chat.postMessage, so each degrades to a
    # labelled mrkdwn link line (caption preferred, filename named for a document).
    http_script.results.append(_ok_response(ts="1.1"))
    media = [
        MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.test/a.pdf", caption="Spec", filename="a.pdf"),
        MediaItem(kind=MediaKind.VIDEO, url="https://cdn.test/clip.mp4", caption="Demo"),
        MediaItem(kind=MediaKind.AUDIO, url="https://cdn.test/note.mp3"),
    ]
    await SlackChannel().notify(ChannelNotification(message="Files:", media=media))

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    assert {"type": "section", "text": {"type": "mrkdwn", "text": "<https://cdn.test/a.pdf|Spec> (a.pdf)"}} in blocks
    assert {"type": "section", "text": {"type": "mrkdwn", "text": "<https://cdn.test/clip.mp4|Demo>"}} in blocks
    assert {"type": "section", "text": {"type": "mrkdwn", "text": "<https://cdn.test/note.mp3|audio>"}} in blocks


async def test_notify_location_renders_section_with_openstreetmap_link(http_script, fake_redis):
    # A shared location renders as a section naming the place with an OpenStreetMap link.
    http_script.results.append(_ok_response(ts="1.1"))
    location = LocationElement(latitude=51.5, longitude=-0.12, name="HQ", address="1 Test St")
    await SlackChannel().notify(ChannelNotification(message="We are here:", location=location))

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    # Inside an ``<url|…>`` link the url's ``&`` is mrkdwn-escaped to ``&amp;``.
    osm = "https://www.openstreetmap.org/?mlat=51.5&amp;mlon=-0.12#map=16/51.5/-0.12"
    assert {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*HQ*\n1 Test St\n<{osm}|View on OpenStreetMap>"},
    } in blocks


async def test_notify_template_is_refused_before_any_send(http_script, fake_redis):
    # Slack has no vendor-template registry, so a template carries only substitution
    # parameters with no skeleton to render — refused loudly, never a meaningless dump.
    template = ChannelTemplate(name="welcome", language="en_US", body_parameters=["Ada"])
    with pytest.raises(ChannelInputError, match="vendor template"):
        await SlackChannel().notify(ChannelNotification(message="Hi", template=template))
    assert http_script.requests == []


async def test_notify_channel_advertises_location_capability():
    # The location send rides supports_location_notifications; the vendor-template capability
    # is honestly NOT advertised (Slack cannot render one).
    assert SlackChannel.supports_location_notifications is True
    assert getattr(SlackChannel, "supports_template_notifications", False) is False
    assert getattr(SlackChannel, "supports_form_notifications", False) is False


async def test_notify_media_renders_image_and_link_blocks(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="1.1"))
    media = [
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption=None),
        MediaItem(kind=MediaKind.LINK, url="https://docs.test/x", caption="doc"),
    ]
    result = await SlackChannel().notify(ChannelNotification(message="See below.", media=media))

    assert result == ["1.1"]
    blocks = json.loads(http_script.requests[0].content)["blocks"]
    assert blocks[0] == {"type": "section", "text": {"type": "plain_text", "text": "See below."}}
    # A captionless image falls back to a generic alt text.
    assert {"type": "image", "image_url": "https://cdn.test/a.png", "alt_text": "image"} in blocks
    assert {"type": "section", "text": {"type": "mrkdwn", "text": "<https://docs.test/x|doc>"}} in blocks


async def test_notify_media_only_posts_image_blocks_without_a_text_section(http_script, fake_redis):
    # A media-only notification (blank message, image only) posts the image block(s) ALONE — no
    # leading text section (Slack rejects an empty plain_text) and an empty text fallback.
    http_script.results.append(_ok_response(ts="2.2"))
    media = [MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption="a chart")]
    result = await SlackChannel().notify(ChannelNotification(message="", media=media))

    assert result == ["2.2"]
    payload = json.loads(http_script.requests[0].content)
    blocks = payload["blocks"]
    assert blocks == [{"type": "image", "image_url": "https://cdn.test/a.png", "alt_text": "a chart"}]
    assert not any(b.get("type") == "section" for b in blocks)  # no text section
    assert payload["text"] == ""  # empty fallback — the blocks carry the content


async def test_notify_data_uri_image_is_refused_before_any_send(http_script, fake_redis):
    media = [MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAA", caption=None)]
    with pytest.raises(ChannelInputError, match="data: image"):
        await SlackChannel().notify(ChannelNotification(message="hi", media=media))
    assert http_script.requests == []


async def test_notify_allowlisted_recipient_sends_to_it(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="1.1"))

    await SlackChannel().notify(ChannelNotification(message="hi", recipient=TEST_ALLOWED_RECIPIENT))

    payload = json.loads(http_script.requests[0].content)
    assert payload["channel"] == TEST_ALLOWED_RECIPIENT


async def test_notify_matching_sender_identity_sends_and_returns_ts(http_script, fake_redis):
    # sender_identity naming this deployment's single bot identity is accepted.
    http_script.results.append(_ok_response(ts="9.9"))

    result = await SlackChannel().notify(ChannelNotification(message="bridge reply", sender_identity=TEST_BOT_USER_ID))

    assert result == ["9.9"]
    assert len(http_script.requests) == 1


async def test_notify_matching_sender_identity_bypasses_recipient_allowlist(http_script, fake_redis):
    # A bridge reply goes to the initiating conversation verbatim — an unlisted
    # recipient is delivered, not refused (the allowlist governs ask only).
    http_script.results.append(_ok_response(ts="7.7"))

    result = await SlackChannel().notify(
        ChannelNotification(message="bridge reply", sender_identity=TEST_BOT_USER_ID, recipient="C0UNLISTED")
    )

    assert result == ["7.7"]
    payload = json.loads(http_script.requests[0].content)
    assert payload["channel"] == "C0UNLISTED"


async def test_notify_foreign_sender_identity_raises_typed_error_nothing_sent(http_script, fake_redis):
    # sender_identity that is not this deployment's identity is refused — never a
    # send from the wrong face.
    with pytest.raises(ChannelDeliveryError, match="is not this channel's identity"):
        await SlackChannel().notify(ChannelNotification(message="hi", sender_identity="U0OTHERBOT"))

    assert http_script.requests == []


async def test_notify_sender_identity_without_bot_user_id_raises_naming_env_var(http_script, fake_redis, monkeypatch):
    # A sender_identity cannot be verified when the single identity is unconfigured.
    monkeypatch.delenv("CHANNEL_SLACK_BOT_USER_ID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_SLACK_BOT_USER_ID"):
        await SlackChannel().notify(ChannelNotification(message="hi", sender_identity=TEST_BOT_USER_ID))

    assert http_script.requests == []


async def test_notify_ok_true_without_ts_raises(http_script, fake_redis):
    # notify now needs the ts to return: an ok body without one is a loud failure.
    http_script.results.append(_ok_response(ts=None))

    with pytest.raises(ChannelDeliveryError, match="no ts"):
        await SlackChannel().notify(ChannelNotification(message="hi"))


async def test_notify_unlisted_recipient_refused_nothing_sent(http_script, fake_redis):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_SLACK_ALLOWED_RECIPIENTS"):
        await SlackChannel().notify(ChannelNotification(message="hi", recipient="C0UNLISTED"))

    assert http_script.requests == []
    assert fake_redis.store == {}


async def test_notify_no_recipient_and_no_default_raises_naming_env_var(http_script, fake_redis, monkeypatch):
    monkeypatch.delenv("CHANNEL_SLACK_DEFAULT_RECIPIENT")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_SLACK_DEFAULT_RECIPIENT"):
        await SlackChannel().notify(ChannelNotification(message="hi"))

    assert http_script.requests == []


async def test_notify_unconfigured_token_raises_naming_env_var(http_script, fake_redis, monkeypatch):
    # notify shares deliver's config contract: a missing bot token is a
    # delivery failure — ChannelDeliveryError naming the env var, raised
    # before any request.
    monkeypatch.delenv("CHANNEL_SLACK_BOT_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_SLACK_BOT_TOKEN"):
        await SlackChannel().notify(ChannelNotification(message="hi"))

    assert http_script.requests == []


async def test_notify_ok_false_raises_with_slack_error(http_script, fake_redis):
    # Slack answers HTTP 200 even for a failed send — the JSON ok field is the
    # only success signal for notify exactly as for deliver.
    http_script.results.append(httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))

    with pytest.raises(ChannelDeliveryError, match="channel_not_found"):
        await SlackChannel().notify(ChannelNotification(message="hi"))

    assert fake_redis.store == {}


@pytest.mark.parametrize("status", [429, 500])
async def test_notify_non_200_status_raises(http_script, fake_redis, status):
    http_script.results.append(httpx.Response(status, json={"ok": False}))

    with pytest.raises(ChannelDeliveryError, match=f"HTTP {status}"):
        await SlackChannel().notify(ChannelNotification(message="hi"))


async def test_rotated_token_lands_on_next_deliver(http_script, fake_redis, monkeypatch):
    http_script.results.append(_ok_response(ts="1.1"))
    http_script.results.append(_ok_response(ts="2.2"))

    await SlackChannel().deliver(make_delivery())
    monkeypatch.setenv("CHANNEL_SLACK_BOT_TOKEN", "xoxb-rotated-token")
    reset_all_settings()
    await SlackChannel().deliver(make_delivery())

    first, second = http_script.requests
    assert first.headers["Authorization"] == f"Bearer {TEST_BOT_TOKEN}"
    assert second.headers["Authorization"] == "Bearer xoxb-rotated-token"
