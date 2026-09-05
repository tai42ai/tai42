"""Outbound sends: recipient resolution (allowlist gate + operator default),
payload shape, the ok-field success signal, Tier-1 vs Tier-2 routing,
correlation writes, notify's plain fire-and-forget path, and every loud
failure branch."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.blocks import (
    LINK_ACTION_PREFIX,
    REPLY_ACTION_PREFIX,
    SELECT_ACTION_PREFIX,
    decode_reply_value,
)
from tai42_channel_slack.channel import SlackChannel, _deliver_form, _render_text, open_modal_view
from tai42_channel_slack.correlation import remaining_seconds
from tai42_channel_slack.forms import build_message_blocks

from .conftest import (
    TEST_ALLOWED_RECIPIENT,
    TEST_BOT_TOKEN,
    TEST_BOT_USER_ID,
    TEST_DEFAULT_RECIPIENT,
    make_delivery,
)

_FORM_SCHEMA = {
    "type": "object",
    "properties": {"full_name": {"type": "string", "title": "Full name"}},
    "required": ["full_name"],
}
_FORM_KEY = "channel:slack:form:int-1"

pytestmark = pytest.mark.usefixtures("slack_env")


def _ok_response(ts: str | None = "1712345678.000100") -> httpx.Response:
    body: dict[str, object] = {"ok": True}
    if ts is not None:
        body["ts"] = ts
    return httpx.Response(200, json=body)


async def test_post_message_payload_shape(http_script, fake_redis):
    http_script.results.append(_ok_response())

    await SlackChannel().deliver(make_delivery())

    (request,) = http_script.requests
    assert str(request.url) == "https://slack.com/api/chat.postMessage"
    assert request.headers["Authorization"] == f"Bearer {TEST_BOT_TOKEN}"
    payload = json.loads(request.content)
    assert payload["channel"] == TEST_DEFAULT_RECIPIENT
    assert payload["text"].startswith("Deploy to production?")


async def test_post_message_url_derives_from_api_base_url(http_script, fake_redis, monkeypatch):
    # An overridden CHANNEL_SLACK_API_BASE_URL (a stub origin in e2e) is where
    # chat.postMessage is addressed — the send URL derives from the setting.
    monkeypatch.setenv("CHANNEL_SLACK_API_BASE_URL", "http://127.0.0.1:9099/api")
    reset_all_settings()
    http_script.results.append(_ok_response())

    await SlackChannel().deliver(make_delivery())

    (request,) = http_script.requests
    assert str(request.url) == "http://127.0.0.1:9099/api/chat.postMessage"


async def test_no_requested_recipient_sends_to_default(http_script, fake_redis):
    http_script.results.append(_ok_response())

    await SlackChannel().deliver(make_delivery(recipient=None))

    payload = json.loads(http_script.requests[0].content)
    assert payload["channel"] == TEST_DEFAULT_RECIPIENT


async def test_allowlisted_recipient_sends_to_it(http_script, fake_redis):
    http_script.results.append(_ok_response())

    await SlackChannel().deliver(make_delivery(recipient=TEST_ALLOWED_RECIPIENT))

    payload = json.loads(http_script.requests[0].content)
    assert payload["channel"] == TEST_ALLOWED_RECIPIENT


@pytest.mark.parametrize(
    "recipient",
    [
        pytest.param("C0UNLISTED", id="unknown-id"),
        # The default recipient is trusted only when the OPERATOR falls back
        # to it; a CALLER naming it is gated by the allowlist like any other
        # requested value.
        pytest.param(TEST_DEFAULT_RECIPIENT, id="default-not-allowlisted"),
    ],
)
async def test_unlisted_recipient_refused_nothing_sent(http_script, fake_redis, recipient):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_SLACK_ALLOWED_RECIPIENTS"):
        await SlackChannel().deliver(make_delivery(recipient=recipient))

    assert http_script.requests == []
    assert fake_redis.store == {}


async def test_empty_allowlist_refuses_every_requested_recipient(http_script, fake_redis, monkeypatch):
    monkeypatch.setenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS", "")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="refusing to send"):
        await SlackChannel().deliver(make_delivery(recipient=TEST_ALLOWED_RECIPIENT))

    assert http_script.requests == []


async def test_no_recipient_and_no_default_raises_naming_env_var(http_script, fake_redis, monkeypatch):
    # Neither a caller-requested recipient nor an operator default: nowhere to
    # deliver, so this operator misconfiguration is a delivery failure —
    # ChannelDeliveryError naming the env var, raised before any request.
    monkeypatch.delenv("CHANNEL_SLACK_DEFAULT_RECIPIENT")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_SLACK_DEFAULT_RECIPIENT"):
        await SlackChannel().deliver(make_delivery(recipient=None))

    assert http_script.requests == []


def test_render_text_select_enumerates_options():
    delivery = make_delivery(answer_format="select", options=["staging", "production"])
    text = _render_text(delivery)
    assert "Options: staging, production" in text
    assert "Reply in this thread." in text
    assert delivery.timeout_at.isoformat() in text


def test_render_text_text_has_no_options_line():
    delivery = make_delivery(answer_format="text")
    text = _render_text(delivery)
    assert "Options:" not in text
    assert "Reply in this thread." in text


async def test_ok_true_stores_correlation_with_budget_ttl(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="123.456"))
    delivery = make_delivery()

    await SlackChannel().deliver(delivery)

    key = "channel:slack:corr:123.456"
    # The corr value is now a JSON {callback_url, interaction_id, timeout_at} record.
    record = json.loads(fake_redis.store[key])
    assert record["callback_url"] == delivery.callback_url
    assert record["interaction_id"] == delivery.interaction_id
    assert fake_redis.ttls[key] == remaining_seconds(delivery.timeout_at)


async def test_ok_false_raises_with_slack_error_and_writes_nothing(http_script, fake_redis):
    # Slack answers HTTP 200 even for a failed send — the JSON ok field is the
    # only success signal.
    http_script.results.append(httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))

    with pytest.raises(ChannelDeliveryError, match="channel_not_found"):
        await SlackChannel().deliver(make_delivery())

    assert fake_redis.store == {}


@pytest.mark.parametrize("status", [429, 500])
async def test_non_200_status_raises(http_script, fake_redis, status):
    http_script.results.append(httpx.Response(status, json={"ok": False}))

    with pytest.raises(ChannelDeliveryError, match=f"HTTP {status}"):
        await SlackChannel().deliver(make_delivery())


async def test_non_json_200_body_raises(http_script, fake_redis):
    http_script.results.append(httpx.Response(200, text="not json"))

    with pytest.raises(ChannelDeliveryError, match="non-JSON body"):
        await SlackChannel().deliver(make_delivery())


async def test_ok_true_without_ts_raises_for_tier2(http_script, fake_redis):
    http_script.results.append(_ok_response(ts=None))

    with pytest.raises(ChannelDeliveryError, match="no ts"):
        await SlackChannel().deliver(make_delivery())


async def test_transport_error_wraps_into_delivery_error(http_script, fake_redis):
    http_script.results.append(httpx.ConnectError("boom"))

    with pytest.raises(ChannelDeliveryError, match="transport failure") as excinfo:
        await SlackChannel().deliver(make_delivery())

    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


async def test_expired_budget_raises_before_any_request(http_script, fake_redis):
    delivery = make_delivery(timeout_at=datetime.now(UTC) - timedelta(seconds=1))

    with pytest.raises(ChannelDeliveryError, match="budget already expired"):
        await SlackChannel().deliver(delivery)

    assert http_script.requests == []


async def test_unconfigured_token_raises_naming_env_var(http_script, fake_redis, monkeypatch):
    # A missing bot token is operator misconfiguration, and on the deliver
    # path that is a delivery failure: ChannelDeliveryError naming the env
    # var, raised before any request.
    monkeypatch.delenv("CHANNEL_SLACK_BOT_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_SLACK_BOT_TOKEN"):
        await SlackChannel().deliver(make_delivery())

    assert http_script.requests == []


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_formats_link_only_no_correlation(http_script, fake_redis, answer_format):
    # An ok:true body WITHOUT ts succeeds for a Tier-1 format — no ts
    # requirement, no correlation write, no threaded reply expected.
    http_script.results.append(_ok_response(ts=None))
    delivery = make_delivery(answer_format=answer_format)

    await SlackChannel().deliver(delivery)

    text = json.loads(http_script.requests[0].content)["text"]
    assert f"Answer here: {delivery.callback_url}" in text
    assert delivery.timeout_at.isoformat() in text
    assert "Reply in this thread" not in text
    assert "Options:" not in text
    assert fake_redis.store == {}


def test_channel_advertises_media_and_interactive_notifications():
    # The central notify guard reads these before handing a media / options
    # notification to this channel.
    assert SlackChannel.supports_media_notifications is True
    assert SlackChannel.supports_interactive_notifications is True


async def test_deliver_select_renders_option_buttons(http_script, fake_redis):
    # A select ask renders its options as a Block Kit actions block of buttons (value =
    # the option text, action_id = tai42_select:<index>); the text keeps the numbered
    # fallback (a thread reply still answers).
    http_script.results.append(_ok_response(ts="1.1"))
    await SlackChannel().deliver(make_delivery(answer_format="select", options=["staging", "production"]))

    payload = json.loads(http_script.requests[0].content)
    blocks = payload["blocks"]
    assert blocks[0] == {"type": "section", "text": {"type": "plain_text", "text": "Deploy to production?"}}
    actions = blocks[-1]
    assert actions["type"] == "actions"
    assert [e["value"] for e in actions["elements"]] == ["staging", "production"]
    assert [e["action_id"] for e in actions["elements"]] == [
        f"{SELECT_ACTION_PREFIX}0",
        f"{SELECT_ACTION_PREFIX}1",
    ]
    # The text fallback still lists the options and the reply-in-thread instruction.
    assert "Options: staging, production" in payload["text"]


async def test_deliver_text_with_suggested_replies_renders_buttons(http_script, fake_redis):
    # A text ask MAY carry suggested replies (contract): they render as the same option
    # buttons, and a tap submits the option text as the free-text answer.
    http_script.results.append(_ok_response(ts="1.1"))
    await SlackChannel().deliver(make_delivery(answer_format="text", options=["yes", "no"]))

    actions = json.loads(http_script.requests[0].content)["blocks"][-1]
    assert [e["value"] for e in actions["elements"]] == ["yes", "no"]


async def test_deliver_media_renders_image_and_link_blocks(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="1.1"))
    media = [
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption="a diagram"),
        MediaItem(kind=MediaKind.LINK, url="https://docs.test/spec", caption="the spec"),
    ]
    await SlackChannel().deliver(make_delivery(media=media))

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    assert blocks[0]["type"] == "section"  # the question
    assert {"type": "image", "image_url": "https://cdn.test/a.png", "alt_text": "a diagram"} in blocks
    assert {"type": "section", "text": {"type": "mrkdwn", "text": "<https://docs.test/spec|the spec>"}} in blocks


async def test_deliver_data_uri_image_is_refused_before_any_send(http_script, fake_redis):
    media = [MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAA", caption=None)]
    with pytest.raises(ChannelInputError, match="data: image"):
        await SlackChannel().deliver(make_delivery(media=media))
    assert http_script.requests == []
    assert fake_redis.store == {}


async def test_deliver_form_posts_blocks_and_stores_record(http_script, fake_redis):
    http_script.results.append(_ok_response(ts="1.1"))
    delivery = make_delivery(answer_format="form", schema=_FORM_SCHEMA)

    await SlackChannel().deliver(delivery)

    (request,) = http_script.requests
    assert str(request.url) == "https://slack.com/api/chat.postMessage"
    payload = json.loads(request.content)
    assert payload["channel"] == TEST_DEFAULT_RECIPIENT
    # text is the notification fallback Slack requires alongside blocks.
    assert payload["text"] == delivery.question
    assert payload["blocks"] == build_message_blocks(delivery.question, delivery.interaction_id)
    record = json.loads(fake_redis.store[_FORM_KEY])
    assert record == {
        "callback_url": delivery.callback_url,
        "schema": _FORM_SCHEMA,
        "question": delivery.question,
        "timeout_at": delivery.timeout_at.isoformat(),
    }
    assert fake_redis.ttls[_FORM_KEY] == remaining_seconds(delivery.timeout_at)
    # The form path never writes a ts-correlation (its answer comes via the modal).
    assert "channel:slack:corr:1.1" not in fake_redis.store


async def test_deliver_form_reserves_record_before_send(stub_app, fake_redis):
    # The reservation must exist at the moment chat.postMessage is invoked, so a
    # click that races the send finds a live record.
    seen: dict[str, bool] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["present"] = _FORM_KEY in fake_redis.store
        return httpx.Response(200, json={"ok": True, "ts": "1.1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stub_app.clients.clients[HttpxClient] = client
    try:
        await SlackChannel().deliver(make_delivery(answer_format="form", schema=_FORM_SCHEMA))
    finally:
        stub_app.clients.clients.pop(HttpxClient, None)
        await client.aclose()

    assert seen["present"] is True


async def test_deliver_form_releases_record_on_send_failure(http_script, fake_redis):
    http_script.results.append(httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))

    with pytest.raises(ChannelDeliveryError, match="channel_not_found"):
        await SlackChannel().deliver(make_delivery(answer_format="form", schema=_FORM_SCHEMA))

    # Reserve-before-send: a failed post releases the reservation.
    assert fake_redis.store == {}


async def test_deliver_form_unmappable_schema_raises_before_any_io(http_script, fake_redis):
    bad = {"type": "object", "properties": {"blob": {"type": "array"}}}

    with pytest.raises(ChannelInputError, match=r"blob.*unsupported type"):
        await SlackChannel().deliver(make_delivery(answer_format="form", schema=bad))

    assert http_script.requests == []
    assert fake_redis.store == {}


async def test_deliver_form_over_cap_modal_raises_before_any_io(http_script, fake_redis):
    # 100 fields → 1 question section + 100 inputs = 101 blocks > the 100-block
    # modal cap. Delivery composes the full modal the click would build, so an
    # uncompletable form is refused before any send or store — never delivered
    # only to 500 when the button is clicked.
    props = {f"f{i}": {"type": "string"} for i in range(100)}
    over_cap = {"type": "object", "properties": props}

    with pytest.raises(ChannelInputError, match="modal exceeds 100 blocks"):
        await SlackChannel().deliver(make_delivery(answer_format="form", schema=over_cap))

    assert http_script.requests == []
    assert fake_redis.store == {}


async def test_deliver_form_without_schema_raises_before_any_io(http_script, fake_redis):
    # The contract guarantees a schema for a form; the defensive guard refuses a
    # schema-less form outright rather than fall back to a plain-text send.
    with pytest.raises(ChannelDeliveryError, match="requires a non-empty schema"):
        await _deliver_form("xoxb-tok", TEST_DEFAULT_RECIPIENT, make_delivery(answer_format="text"))

    assert http_script.requests == []
    assert fake_redis.store == {}


async def test_validate_form_schema_hook_mirrors_delivery_refusal(http_script, fake_redis):
    # 150 enum options exceed Slack's 100 static-select cap. The ask-time hook
    # refuses the schema (ValueError) for the same limit the delivery path refuses
    # (ChannelInputError): one rule, two doors — so a schema Block Kit could never
    # render is rejected before any state is written rather than persisted and
    # failed at delivery.
    schema = {"type": "object", "properties": {"pick": {"type": "string", "enum": [str(i) for i in range(150)]}}}
    channel = SlackChannel()

    with pytest.raises(ValueError, match="enum exceeds 100 options"):
        channel.validate_form_schema(schema, "q")

    with pytest.raises(ChannelInputError, match="enum exceeds 100 options"):
        await channel.deliver(make_delivery(answer_format="form", schema=schema))

    assert http_script.requests == []
    assert fake_redis.store == {}


async def test_validate_form_schema_hook_refuses_over_long_question(http_script, fake_redis):
    # The 3000-char section cap on the question is knowable at ask-time, so the hook
    # refuses an over-long question (ValueError) up front — nothing is sent, nothing
    # is persisted — rather than the delivery path pruning it after the fact.
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    channel = SlackChannel()

    with pytest.raises(ValueError, match="question exceeds"):
        channel.validate_form_schema(schema, "x" * 3001)

    assert http_script.requests == []
    assert fake_redis.store == {}


@pytest.mark.parametrize(
    ("result", "match"),
    [
        pytest.param(httpx.ConnectError("boom"), "transport failure", id="transport"),
        pytest.param(httpx.Response(502, json={"ok": False}), "HTTP 502", id="non-200"),
        pytest.param(httpx.Response(200, text="not json"), "non-JSON body", id="non-json"),
    ],
)
async def test_open_modal_view_failure_branches_raise(http_script, result, match):
    http_script.results.append(result)

    with pytest.raises(ChannelDeliveryError, match=match):
        await open_modal_view("trg-1", {"type": "modal"})


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
        ReplyOption(text="Refund", description="Money back to your card"),
        ReplyOption(text="Replace"),
    ]
    await SlackChannel().notify(ChannelNotification(message="Choose:", options=options))

    blocks = json.loads(http_script.requests[0].content)["blocks"]
    context = next(b for b in blocks if b["type"] == "context")
    assert context["elements"] == [{"type": "mrkdwn", "text": "*Refund* — Money back to your card"}]
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
    osm = "https://www.openstreetmap.org/?mlat=51.5&mlon=-0.12#map=16/51.5/-0.12"
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
    # recipient is delivered, not refused (the allowlist governs ask_user only).
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
