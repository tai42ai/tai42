"""Outbound sends: recipient resolution (allowlist gate + operator default),
payload shape, the ok-field success signal, Tier-1 vs Tier-2 routing,
correlation writes, notify's plain fire-and-forget path, and every loud
failure branch."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelNotification
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.channel import SlackChannel, _deliver_form, _render_text, open_modal_view
from tai42_channel_slack.correlation import remaining_seconds
from tai42_channel_slack.forms import build_message_blocks
from tests.conftest import (
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
    assert fake_redis.store[key] == delivery.callback_url
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

    with pytest.raises(ChannelDeliveryError, match=r"blob.*unsupported type"):
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

    with pytest.raises(ChannelDeliveryError, match="modal exceeds 100 blocks"):
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
