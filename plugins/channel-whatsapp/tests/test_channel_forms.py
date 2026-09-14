"""WhatsApp-Flow form send — the form ask (create/publish/cache/send, reservation,
orphan-draft cleanup) and the ask-less form notification (namespaced token, no
reservation, schema sidecar)."""

from __future__ import annotations

import logging

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError, ChannelNotification
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.flows import build_flow_data, build_form_flow

from .conftest import (
    _MESSAGES_URL,
    ALLOWED_A,
    ALLOWED_B,
    PHONE_NUMBER_ID,
    FakeHttpx,
    FakeRedis,
    _accepted,
    make_delivery,
    response,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_WABA_ID = "WABA-100"
_FORM_SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}, "qty": {"type": "integer"}},
    "required": ["note"],
}


# --- Form delivery (WhatsApp Flows) -------------------------------------------


@pytest.fixture
def waba_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", _WABA_ID)
    reset_all_settings()


def _flow_created(flow_id: str = "flow-1") -> httpx.Response:
    return response(200, json={"id": flow_id})


def _published() -> httpx.Response:
    return response(200, json={"success": True})


def _form_delivery(**overrides):
    fields = {"answer_format": "form", "schema": _FORM_SCHEMA, "question": "Please fill this in."}
    fields.update(overrides)
    return make_delivery(**fields)


def _flow_cache_key(schema_hash: str) -> str:
    return f"channel:whatsapp:flow:{_WABA_ID}:{schema_hash}"


async def test_form_cache_miss_creates_publishes_then_sends(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    flow_json, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert [call["url"] for call in fake_httpx.calls] == [
        f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows",
        "https://graph.facebook.com/v23.0/flow-1/publish",
        _MESSAGES_URL,
    ]
    # create_flow: exact wire shape, flow_json serialized as a JSON string.
    import json

    create = fake_httpx.calls[0]["json"]
    assert create == {
        "name": f"tai42-form-{schema_hash}",
        "categories": ["OTHER"],
        "flow_json": json.dumps(flow_json),
    }
    assert fake_httpx.calls[1]["json"] == {}  # publish carries no body
    # send_flow: exact interactive-flow payload, flow_token = the interaction id.
    send = fake_httpx.calls[2]["json"]
    assert send == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": "Please fill this in."},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": "int-1",
                    "flow_id": "flow-1",
                    "flow_cta": "Fill form",
                    "flow_action": "navigate",
                    # A per-send form navigates to the entry screen and injects the
                    # prefill/option data (here the empty defaults — no per-send data).
                    "flow_action_payload": {
                        "screen": "SCREEN_0",
                        "data": build_flow_data(_FORM_SCHEMA, {}, {}),
                    },
                },
            },
        },
    }
    # The published flow id is cached (no TTL) and the pending ask carries the schema.
    assert fake_redis.store[_flow_cache_key(schema_hash)] == "flow-1"
    assert _flow_cache_key(schema_hash) not in fake_redis.ttls
    stored = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert stored["schema"] == _FORM_SCHEMA
    assert stored["interaction_id"] == "int-1"


async def test_form_with_pages_and_data_publishes_once_and_carries_data_per_send(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormData, FormOption, FormPage

    schema = {
        "type": "object",
        "properties": {"tier": {"type": "string", "enum": ["g", "s"]}, "note": {"type": "string"}},
    }
    data = FormData(values={"note": "hi"}, options={"tier": [FormOption(value="g", label="Gold")]})
    pages = [FormPage(title="Plan", fields=["tier"]), FormPage(title="Say", fields=["note"])]
    # The publish key carries the option-bearing set — here ``{"tier"}`` from the ask's data.
    _, ask_hash = build_form_flow(
        schema, [{"title": "Plan", "fields": ["tier"]}, {"title": "Say", "fields": ["note"]}], {"tier"}
    )

    # First send: cache miss → create + publish + send. Second send: cache hit → send only.
    fake_httpx.responses.append(_flow_created("flow-multi"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.ONE"))
    fake_httpx.responses.append(_accepted("wamid.TWO"))

    delivery = _form_delivery(schema=schema, data=data, pages=pages)
    await WhatsAppChannel().deliver(delivery)
    # A second delivery (a different pair) reuses the cached published Flow.
    await WhatsAppChannel().deliver(
        _form_delivery(schema=schema, data=data, pages=pages, interaction_id="int-2", recipient=ALLOWED_B)
    )

    # One create+publish for the (schema, pages) pair, then a send per delivery — never a
    # new Flow per send.
    urls = [call["url"] for call in fake_httpx.calls]
    assert urls == [
        f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows",
        "https://graph.facebook.com/v23.0/flow-multi/publish",
        _MESSAGES_URL,
        _MESSAGES_URL,
    ]
    assert fake_redis.store[_flow_cache_key(ask_hash)] == "flow-multi"
    # The send carries the per-send values/options through the Flow action payload's data.
    action_payload = fake_httpx.calls[2]["json"]["interactive"]["action"]["parameters"]["flow_action_payload"]
    assert action_payload["screen"] == "SCREEN_0"
    assert action_payload["data"]["note__init"] == "hi"
    assert action_payload["data"]["tier__ds"] == [{"id": "g", "title": "Gold"}]


async def test_form_per_send_option_on_non_string_field_raises_before_any_send(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormData, FormOption

    # A plain string takes per-send options (parity with web/Slack); a non-string never
    # can — only a string maps to a dropdown — so it is refused before any network work.
    schema = {"type": "object", "properties": {"agree": {"type": "boolean"}}}
    data = FormData(values={}, options={"agree": [FormOption(value="x")]})
    with pytest.raises(ChannelInputError, match="agree"):
        await WhatsAppChannel().deliver(_form_delivery(schema=schema, data=data))
    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_form_cache_hit_sends_only(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    # No create/publish — only the send, referencing the cached flow id.
    assert len(fake_httpx.calls) == 1
    assert fake_httpx.calls[0]["url"] == _MESSAGES_URL
    assert fake_httpx.calls[0]["json"]["interactive"]["action"]["parameters"]["flow_id"] == "flow-cached"


async def test_form_reserves_before_any_send(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    kinds = [kind for kind, _ in fake_redis.events]
    assert kinds.index("redis_set") < kinds.index("http_post")


async def test_form_send_failure_releases_reservation(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(response(400, json={"error": {"code": 131009, "message": "Invalid recipient"}}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*131009"):
        await WhatsAppChannel().deliver(_form_delivery())

    # The pair was released — a follow-up form deliver reserves and sends cleanly.
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store
    fake_httpx.responses.append(_accepted("wamid.OK"))
    await WhatsAppChannel().deliver(_form_delivery())


async def test_form_create_failure_releases_reservation(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A create/publish failure after the reservation frees the pair and raises.
    fake_httpx.responses.append(response(500, text="graph down"))

    with pytest.raises(ChannelDeliveryError, match="HTTP 500"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_form_publish_failure_deletes_orphan_draft_and_raises(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Create succeeds, publish fails — the stranded draft is deleted by its id and
    # the original publish error surfaces; nothing is cached, the pair is freed.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(response(500, text="publish down"))
    fake_httpx.responses.append(_published())  # delete succeeds (its 2xx body is unused)

    with pytest.raises(ChannelDeliveryError, match="publish down"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert fake_httpx.calls[-1]["url"] == "https://graph.facebook.com/v23.0/flow-1"
    assert ("http_delete", "https://graph.facebook.com/v23.0/flow-1") in fake_httpx.events
    assert _flow_cache_key(schema_hash) not in fake_redis.store
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_form_orphan_delete_failure_is_logged_and_original_error_raised(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # Publish fails AND the cleanup delete fails: the delete failure is logged
    # without masking, and the ORIGINAL publish error is the one that surfaces.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(response(500, text="publish down"))
    fake_httpx.responses.append(response(500, text="delete down"))

    with (
        caplog.at_level(logging.ERROR, logger="tai42_channel_whatsapp.channel.forms"),
        pytest.raises(ChannelDeliveryError, match="publish down"),
    ):
        await WhatsAppChannel().deliver(_form_delivery())

    assert any("orphaned draft flow flow-1" in record.message for record in caplog.records)
    assert _flow_cache_key(schema_hash) not in fake_redis.store


async def test_form_missing_waba_id_raises_loudly(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # whatsapp_env sets no WABA id — the form path refuses loudly, naming the env var.
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_WABA_ID"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert not fake_httpx.calls
    assert not fake_redis.store  # nothing reserved


async def test_form_unsupported_schema_raises_before_any_network(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    bad_schema = {"type": "object", "properties": {"widget": {"type": "object"}}, "required": []}

    with pytest.raises(ChannelInputError, match="'widget'"):
        await WhatsAppChannel().deliver(_form_delivery(schema=bad_schema))

    assert not fake_httpx.calls  # no create/publish/send
    assert not fake_redis.store  # no reservation, no flow cache
    assert not fake_redis.events  # not a single Redis write either


async def test_validate_form_schema_hook_mirrors_delivery_refusal(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The ask-time hook refuses the reserved ``flow_token`` property (ValueError)
    # for the same schema the delivery path refuses (ChannelInputError): one rule,
    # two doors, so a schema the Flow could never carry is rejected before any
    # state is written rather than persisted and failed at delivery.
    schema = {"type": "object", "properties": {"flow_token": {"type": "string"}}, "required": []}
    channel = WhatsAppChannel()

    with pytest.raises(ValueError, match=r"'flow_token'.*reserved"):
        channel.validate_form_schema(schema, "q")

    with pytest.raises(ChannelInputError, match=r"'flow_token'.*reserved"):
        await channel.deliver(_form_delivery(schema=schema))

    assert not fake_httpx.calls  # no create/publish/send
    assert not fake_redis.store  # no reservation, no flow cache


async def test_validate_form_schema_hook_refuses_over_long_question(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The Flow body is ``interactive.body.text``, capped by Meta at 1024 chars, so an
    # over-long question is knowable at ask-time: the hook refuses it (ValueError) up
    # front — nothing sent, nothing persisted — rather than the delivery path pruning
    # it after Meta rejects the send.
    channel = WhatsAppChannel()

    with pytest.raises(ValueError, match="question exceeds"):
        channel.validate_form_schema(_FORM_SCHEMA, "x" * 1025)

    assert not fake_httpx.calls  # no create/publish/send
    assert not fake_redis.store  # no reservation, no flow cache

    channel.validate_form_schema(_FORM_SCHEMA, "x" * 1024)  # at the cap: passes the hook


async def test_form_create_without_id_raises_and_releases(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A 2xx flow-create that carries no id is a loud failure (mirrors no-message-id).
    fake_httpx.responses.append(response(200, json={}))

    with pytest.raises(ChannelDeliveryError, match="no flow id"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_channel_advertises_form_delivery_capability():
    assert WhatsAppChannel.supports_form_delivery is True


# --- Ask-less form notifications (notify with schema) -------------------------

# The wire-visible token namespace for an ask-less form's flow token, pinned as a
# LITERAL: inbound routing branches on this prefix, so changing it orphans every
# form already sitting in a chat.
_NF_PREFIX = "tai42-nf:"


def _schema_cache_key(schema_hash: str) -> str:
    return f"channel:whatsapp:flow-schema:{_WABA_ID}:{schema_hash}"


def _form_notification(**overrides):
    fields: dict = {"message": "Please fill this in.", "recipient": ALLOWED_A, "schema": _FORM_SCHEMA}
    fields.update(overrides)
    return ChannelNotification(**fields)


async def test_notify_form_sends_media_prelude_then_flow_last_no_reservation(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.LINKS"))
    fake_httpx.responses.append(_accepted("wamid.IMG"))
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    ids = await WhatsAppChannel().notify(
        _form_notification(
            media=[
                MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Details page"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg", caption="Front"),
            ]
        )
    )

    assert ids == ["wamid.LINKS", "wamid.IMG", "wamid.FLOW"]  # every wamid, in send order
    # Media rides ahead: the link line-block, then each image — the Flow message LAST,
    # so the actionable prompt sits at the foot of the chat.
    assert fake_httpx.calls[0]["json"]["type"] == "text"
    assert fake_httpx.calls[0]["json"]["text"]["body"] == "Details page: https://docs.example/p/1"
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/a.jpg", "caption": "Front"}
    send = fake_httpx.calls[2]["json"]
    assert send["type"] == "interactive"
    assert send["interactive"]["body"]["text"] == "Please fill this in."
    assert send["interactive"]["action"]["parameters"]["flow_id"] == "flow-cached"
    # The token is namespaced: prefix + schema hash + a random suffix.
    token = send["interactive"]["action"]["parameters"]["flow_token"]
    assert token.startswith(f"{_NF_PREFIX}{schema_hash}:")
    assert len(token) > len(f"{_NF_PREFIX}{schema_hash}:")
    # NO reservation of any kind: nothing pending on the pair, ever.
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store
    assert not [key for _, key in fake_redis.events if key.startswith("channel:whatsapp:pending:")]
    # The answer schema is cached durably beside the flow id (no TTL): the inbound
    # reply carries only the hash and cannot repopulate this entry.
    assert json.loads(fake_redis.store[_schema_cache_key(schema_hash)]) == _FORM_SCHEMA
    assert _schema_cache_key(schema_hash) not in fake_redis.ttls


async def test_notify_form_cache_miss_creates_publishes_then_sends(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The one-published-Flow-per-schema resolve is the SAME machinery the form ask uses.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.responses.append(response(200, json={"id": "flow-new"}))
    fake_httpx.responses.append(response(200, json={"success": True}))
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    ids = await WhatsAppChannel().notify(_form_notification())

    assert ids == ["wamid.FLOW"]
    assert [call["url"] for call in fake_httpx.calls] == [
        f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows",
        "https://graph.facebook.com/v23.0/flow-new/publish",
        _MESSAGES_URL,
    ]
    assert fake_redis.store[_flow_cache_key(schema_hash)] == "flow-new"


async def test_notify_form_token_disjoint_from_ask_flow_tokens(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Both directions of the namespace fence: an ask's flow token is its interaction id
    # verbatim (no prefix), a notification's is always prefixed — and two notifications
    # for the SAME schema still mint distinct tokens (the random suffix).
    # The ask and the ask-less form build the SAME per-send Flow (keyed on the
    # (schema, pages, option_fields) triple), so for one schema+layout they REUSE one
    # published Flow — the per-send flow token, not the Flow, keeps their replies disjoint.
    _, shared_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(shared_hash)] = "flow-shared"
    fake_httpx.responses.append(_accepted("wamid.ASK"))
    fake_httpx.responses.append(_accepted("wamid.NF1"))
    fake_httpx.responses.append(_accepted("wamid.NF2"))

    await WhatsAppChannel().deliver(_form_delivery())
    await WhatsAppChannel().notify(_form_notification(recipient=ALLOWED_B))
    await WhatsAppChannel().notify(_form_notification(recipient=ALLOWED_B))

    tokens = [call["json"]["interactive"]["action"]["parameters"]["flow_token"] for call in fake_httpx.calls]
    ask_token, notify_one, notify_two = tokens
    assert ask_token == "int-1"  # the delivery's interaction id, verbatim
    assert not ask_token.startswith(_NF_PREFIX)
    assert notify_one.startswith(_NF_PREFIX)
    assert notify_two.startswith(_NF_PREFIX)
    assert notify_one != notify_two  # replay-distinct per send
    assert ask_token not in (notify_one, notify_two)


async def test_notify_form_missing_waba_id_raises_loudly(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_WABA_ID"):
        await WhatsAppChannel().notify(_form_notification())

    assert not fake_httpx.calls


async def test_notify_form_prefill_and_pages_reach_send_flow(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An ask-less form's per-send prefill/options and step layout ride the send exactly as a
    # form ask's do: the notify Flow is the dynamic per-send Flow (keyed on the
    # (schema, pages, option_fields) triple), and the send navigates to the entry screen
    # injecting the values/options — so the participant's form opens already filled in.
    from tai42_contract.interactions.models import FormData, FormOption, FormPage

    schema = {
        "type": "object",
        "properties": {"tier": {"type": "string", "enum": ["g", "s"]}, "note": {"type": "string"}},
    }
    data = FormData(values={"note": "hi"}, options={"tier": [FormOption(value="g", label="Gold")]})
    pages = [FormPage(title="Plan", fields=["tier"]), FormPage(title="Say", fields=["note"])]
    _, schema_hash = build_form_flow(
        schema, [{"title": "Plan", "fields": ["tier"]}, {"title": "Say", "fields": ["note"]}], {"tier"}
    )
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-nf-prefill"
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    ids = await WhatsAppChannel().notify(_form_notification(schema=schema, data=data, pages=pages))

    assert ids == ["wamid.FLOW"]
    params = fake_httpx.calls[0]["json"]["interactive"]["action"]["parameters"]
    assert params["flow_id"] == "flow-nf-prefill"
    assert params["flow_token"].startswith(f"{_NF_PREFIX}{schema_hash}:")
    action_payload = params["flow_action_payload"]
    assert action_payload["screen"] == "SCREEN_0"
    # The prefill value and the per-send option list reach the Flow's screen data model.
    assert action_payload["data"]["note__init"] == "hi"
    assert action_payload["data"]["tier__ds"] == [{"id": "g", "title": "Gold"}]


async def test_channel_advertises_form_notification_capability():
    assert WhatsAppChannel.supports_form_notifications is True
