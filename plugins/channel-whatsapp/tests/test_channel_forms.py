"""WhatsApp-Flow form send — the form ask (create/publish/cache/send, reservation,
orphan-draft cleanup) and the ask-less form notification (namespaced token, no
reservation, schema sidecar)."""

from __future__ import annotations

import logging

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError, ChannelNotification
from tai42_contract.interactions.models import (
    AnswerMismatchPolicy,
    AnswerWording,
    FormWording,
    MediaItem,
    MediaKind,
    Wording,
)

from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.flows import build_flow_data, build_form_flow, payload_labels

from .conftest import (
    _MESSAGES_URL,
    ALLOWED_A,
    ALLOWED_B,
    PHONE_NUMBER_ID,
    FakeHttpx,
    FakeRedis,
    _accepted,
    expected_flow_identity,
    expected_flow_name,
    flow_list_page,
    listed_flow,
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


def _http_order(fake_httpx: FakeHttpx) -> list[tuple[str, str]]:
    """Every Graph call in the order it was made, across GET/POST/DELETE."""
    return [(kind.removeprefix("http_"), url) for kind, url in fake_httpx.events if kind.startswith("http_")]


def _flow_cache_key(schema_hash: str) -> str:
    return f"channel:whatsapp:flow:{_WABA_ID}:{expected_flow_identity(schema_hash)}"


async def test_form_cache_miss_creates_publishes_then_sends(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    flow_json, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [
        ("get", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", "https://graph.facebook.com/v23.0/flow-1/publish"),
        ("post", _MESSAGES_URL),
    ]
    # create_flow: exact wire shape, flow_json serialized as a JSON string.
    import json

    create = fake_httpx.calls[0]["json"]
    assert create == {
        "name": expected_flow_name(schema_hash),
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
                        "screen": "SCREEN_A",
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
    # The publish key carries the option-bearing set — here ``{"tier"}`` from the ask's data —
    # and the pages exactly as the channel renders them (each with its input kind + empty display).
    _, ask_hash = build_form_flow(
        schema,
        [
            {"title": "Plan", "fields": ["tier"], "kind": "input", "display": []},
            {"title": "Say", "fields": ["note"], "kind": "input", "display": []},
        ],
        {"tier"},
    )

    # First send: cache miss → create + publish + send. Second send: cache hit → send only.
    fake_httpx.get_responses.append(flow_list_page())
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
    assert _http_order(fake_httpx) == [
        ("get", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", "https://graph.facebook.com/v23.0/flow-multi/publish"),
        ("post", _MESSAGES_URL),
        ("post", _MESSAGES_URL),
    ]
    assert fake_redis.store[_flow_cache_key(ask_hash)] == "flow-multi"
    # The send carries the per-send values/options through the Flow action payload's data.
    action_payload = fake_httpx.calls[2]["json"]["interactive"]["action"]["parameters"]["flow_action_payload"]
    assert action_payload["screen"] == "SCREEN_A"
    assert action_payload["data"]["note__init"] == "hi"
    assert action_payload["data"]["tier__ds"] == [{"id": "g", "title": "Gold"}]


async def test_form_send_carries_a_per_send_option_second_line_in_the_data_source(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormData, FormOption

    # A per-send option's second line (``description``) reaches the send's data-source item, so
    # the choice control draws it under the option title.
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["g", "s"]}}}
    data = FormData(
        values={},
        options={"tier": [FormOption(value="g", label="Gold", description="2-3 days"), FormOption(value="s")]},
    )
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-desc"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.ONE"))

    await WhatsAppChannel().deliver(_form_delivery(schema=schema, data=data))

    action_payload = fake_httpx.calls[2]["json"]["interactive"]["action"]["parameters"]["flow_action_payload"]
    assert action_payload["data"]["tier__ds"] == [
        {"id": "g", "title": "Gold", "description": "2-3 days"},
        {"id": "s", "title": "s"},
    ]


async def test_form_send_folds_a_chip_option_second_line_onto_the_title(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormData, FormOption

    # On a chip grid the option's second line is folded onto the chip title (a ChipsSelector item has
    # no description slot), so the send's data-source row carries no ``description``.
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["g", "s", "b"], "control": "chips"}}}
    data = FormData(
        values={},
        options={"tier": [FormOption(value="g", label="Gold", description="2-3 days"), FormOption(value="s")]},
    )
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-chips"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.ONE"))

    await WhatsAppChannel().deliver(_form_delivery(schema=schema, data=data))

    action_payload = fake_httpx.calls[2]["json"]["interactive"]["action"]["parameters"]["flow_action_payload"]
    assert action_payload["data"]["tier__ds"] == [
        {"id": "g", "title": "Gold \u2014 2-3 days"},
        {"id": "s", "title": "s"},
    ]


async def test_form_send_with_a_chips_option_list_past_meta_max_is_refused_before_any_flow_work(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormData, FormOption

    # A chips field (3-entry enum, drawn as a ChipsSelector) sent with 21 per-send options is refused
    # loudly before any Flow is created, published or sent.
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["g", "s", "b"], "control": "chips"}}}
    data = FormData(values={}, options={"tier": [FormOption(value=f"v{n}") for n in range(21)]})
    with pytest.raises(ChannelInputError, match=r"'tier'.*21 per-send options.*2\.\.20"):
        await WhatsAppChannel().deliver(_form_delivery(schema=schema, data=data))
    assert fake_httpx.calls == []


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


async def test_form_open_label_draws_the_flow_cta_and_round_trips_on_the_pending_record(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery(wording=Wording(form=FormWording(open="Begin"))))

    params = fake_httpx.calls[0]["json"]["interactive"]["action"]["parameters"]
    assert params["flow_cta"] == "Begin"
    # The open label is a message parameter, not Flow JSON — the cached flow id is reused (no republish).
    assert params["flow_id"] == "flow-cached"
    # The whole wording catalogue round-trips on the pending record, so a re-send reproduces it.
    stored = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert stored["wording"]["form"]["open"] == "Begin"


async def test_form_open_label_over_cap_refused_before_any_reserve_or_send(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    with pytest.raises(ChannelInputError, match="flow_cta"):
        await WhatsAppChannel().deliver(_form_delivery(wording=Wording(form=FormWording(open="x" * 21))))

    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_form_title_is_the_page_less_screen_title_and_rekeys_the_flow(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    flow_json, schema_hash = build_form_flow(_FORM_SCHEMA, form_title="Your details")
    assert flow_json["screens"][0]["title"] == "Your details"
    # The titled flow keys its own published Flow (distinct from the untitled one), so a cache miss
    # creates+publishes against the titled hash.
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-titled"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery(wording=Wording(form=FormWording(title="Your details"))))

    create = fake_httpx.calls[0]["json"]
    assert json.loads(create["flow_json"])["screens"][0]["title"] == "Your details"
    assert fake_redis.store[_flow_cache_key(schema_hash)] == "flow-titled"
    stored = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert stored["wording"]["form"]["title"] == "Your details"


async def test_form_submit_draws_the_page_less_footer_and_rekeys_the_flow(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    # A page-less form's terminal button reads ``wording.form.submit``: it is the one resolved
    # page's Footer.label (Flow JSON), so it re-keys the published Flow (distinct from the default).
    flow_json, schema_hash = build_form_flow(_FORM_SCHEMA, form_submit="Send it")
    footer = flow_json["screens"][0]["layout"]["children"][-1]
    assert footer["type"] == "Footer"
    assert footer["label"] == "Send it"
    _, default_hash = build_form_flow(_FORM_SCHEMA)
    assert schema_hash != default_hash  # the submit label re-keys the Flow

    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-submit"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery(wording=Wording(form=FormWording(submit="Send it"))))

    create = fake_httpx.calls[0]["json"]
    created_footer = json.loads(create["flow_json"])["screens"][0]["layout"]["children"][-1]
    assert created_footer["label"] == "Send it"
    assert fake_redis.store[_flow_cache_key(schema_hash)] == "flow-submit"
    stored = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert stored["wording"]["form"]["submit"] == "Send it"


async def test_form_submit_over_cap_refused_before_any_send(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The page-less submit label rides Meta's Footer.label, capped at 35; 36 is refused loudly at
    # build, before any reservation or send.
    with pytest.raises(ChannelInputError, match="footer label"):
        await WhatsAppChannel().deliver(_form_delivery(wording=Wording(form=FormWording(submit="z" * 36))))

    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_form_rejected_template_over_cap_refused_before_any_reserve_or_send(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The caller's refusal sentence, with its one ``{reason}`` token filled by the door reason (up to
    # 500 chars), must fit the 1024-char Flow body: a one-token template whose fixed text exceeds
    # 1024 - 500 = 524 overflows on the re-send and is refused at deliver.
    over = "x" * 525 + "{reason}"
    with pytest.raises(ChannelInputError, match=r"wording\.answer\.rejected"):
        await WhatsAppChannel().deliver(_form_delivery(wording=Wording(answer=AnswerWording(rejected=over))))

    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_form_rejected_template_with_repeated_reason_token_bounded_by_worst_case(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Each ``{reason}`` token is filled, so the cap bounds the WORST-CASE expansion, not a single
    # stripping: a template whose fixed text fits alone but whose two filled tokens push the body past
    # 1024 (520 + 2*500 = 1520) is refused at deliver, never stranding the participant on the re-send.
    two_tokens = "x" * 520 + "{reason}{reason}"
    with pytest.raises(ChannelInputError, match=r"wording\.answer\.rejected"):
        await WhatsAppChannel().deliver(_form_delivery(wording=Wording(answer=AnswerWording(rejected=two_tokens))))

    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_form_rejected_template_at_cap_boundary_is_accepted(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    # Exactly the worst-case boundary passes: 524 fixed chars + one filled {reason} (500) = 1024.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    template = "x" * 524 + "{reason}"
    await WhatsAppChannel().deliver(_form_delivery(wording=Wording(answer=AnswerWording(rejected=template))))

    stored = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert stored["wording"]["answer"]["rejected"] == template


async def test_form_reservation_carries_on_mismatch_and_wording(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    import json

    # A form ask reserves its digression policy and the whole wording catalogue, so a door-rejection
    # re-send reproduces every per-send text and the ladder reads the policy off the record.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(
        _form_delivery(
            on_mismatch=AnswerMismatchPolicy.BRIDGE,
            wording=Wording(form=FormWording(open="Begin"), answer=AnswerWording(rejected="Nope: {reason}")),
        )
    )

    stored = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert stored["on_mismatch"] == "bridge"
    assert stored["wording"]["form"]["open"] == "Begin"
    assert stored["wording"]["answer"]["rejected"] == "Nope: {reason}"


async def test_notify_form_open_label_draws_the_flow_cta(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    await WhatsAppChannel().notify(_form_notification(wording=Wording(form=FormWording(open="Open form"))))

    params = fake_httpx.calls[-1]["json"]["interactive"]["action"]["parameters"]
    assert params["flow_cta"] == "Open form"


async def test_notify_form_submit_draws_the_page_less_footer(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    import json

    # An ask-less form's page-less submit label draws the Footer exactly as a form ask's does: it
    # re-keys the published Flow, so a cache miss creates against the submit-keyed hash and the
    # created Flow JSON's terminal Footer carries the caller's label.
    _, schema_hash = build_form_flow(_FORM_SCHEMA, form_submit="Send it")
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-nf-submit"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    await WhatsAppChannel().notify(_form_notification(wording=Wording(form=FormWording(submit="Send it"))))

    create = fake_httpx.calls[0]["json"]
    footer = json.loads(create["flow_json"])["screens"][0]["layout"]["children"][-1]
    assert footer["type"] == "Footer"
    assert footer["label"] == "Send it"
    assert fake_redis.store[_flow_cache_key(schema_hash)] == "flow-nf-submit"


async def test_notify_form_open_label_over_cap_refused_before_send(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    with pytest.raises(ChannelInputError, match="flow_cta"):
        await WhatsAppChannel().notify(_form_notification(wording=Wording(form=FormWording(open="y" * 21))))

    assert fake_httpx.calls == []


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
    fake_httpx.get_responses.append(flow_list_page())
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
    fake_httpx.get_responses.append(flow_list_page())
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
    fake_httpx.get_responses.append(flow_list_page())
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


async def test_form_create_with_validation_errors_refuses_and_deletes_draft(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Meta returns HTTP 200 for a create even when the Flow JSON is invalid: the draft
    # is created but carries validation_errors and can never publish. _resolve_flow_id
    # refuses loudly with the rendered list BEFORE publish, deletes the stranded draft
    # through its one cleanup writer, publishes/caches nothing, and frees the pair.
    import json

    errors = [{"error_type": "INVALID_PROPERTY", "message": "id has a digit", "pointer": "/screens/0/id"}]
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(response(200, json={"id": "flow-bad", "validation_errors": errors}))
    fake_httpx.responses.append(_published())  # the cleanup delete (its 2xx body is unused)

    with pytest.raises(ChannelDeliveryError, match="validation errors and it can never publish") as excinfo:
        await WhatsAppChannel().deliver(_form_delivery())

    # The full rendered vendor list rides the message.
    assert json.dumps(errors, sort_keys=True, separators=(",", ":")) in str(excinfo.value)
    # Create then delete of the stranded draft — never a publish.
    assert _http_order(fake_httpx) == [
        ("get", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("delete", "https://graph.facebook.com/v23.0/flow-bad"),
    ]
    assert ("http_delete", "https://graph.facebook.com/v23.0/flow-bad") in fake_httpx.events
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    assert _flow_cache_key(schema_hash) not in fake_redis.store  # nothing cached
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store  # pair freed


async def test_form_clean_create_still_publishes_and_caches(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A create with no validation_errors (an empty array is the clean case) publishes
    # and caches exactly as before — the refusal is scoped to a non-empty list.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(response(200, json={"id": "flow-ok", "validation_errors": []}))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [
        ("get", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", "https://graph.facebook.com/v23.0/flow-ok/publish"),
        ("post", _MESSAGES_URL),
    ]
    assert fake_redis.store[_flow_cache_key(schema_hash)] == "flow-ok"


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
    fake_httpx.get_responses.append(flow_list_page())
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

    outcome = await WhatsAppChannel().notify(
        _form_notification(
            media=[
                MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Details page"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg", caption="Front"),
            ]
        )
    )

    assert outcome.message_ids == ["wamid.LINKS", "wamid.IMG", "wamid.FLOW"]  # every wamid, in send order
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
    # reply carries only the hash and cannot repopulate this entry. The payload-label
    # reverse map rides with it so the reply decodes to schema keys.
    expected_labels = {label: key for key, label in payload_labels(_FORM_SCHEMA["properties"]).items()}
    assert json.loads(fake_redis.store[_schema_cache_key(schema_hash)]) == {
        "schema": _FORM_SCHEMA,
        "names": expected_labels,
        "pages": None,
        "option_fields": [],
    }
    assert _schema_cache_key(schema_hash) not in fake_redis.ttls


async def test_notify_form_cache_miss_creates_publishes_then_sends(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The one-published-Flow-per-schema resolve is the SAME machinery the form ask uses.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(response(200, json={"id": "flow-new"}))
    fake_httpx.responses.append(response(200, json={"success": True}))
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    outcome = await WhatsAppChannel().notify(_form_notification())

    assert outcome.message_ids == ["wamid.FLOW"]
    assert _http_order(fake_httpx) == [
        ("get", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"),
        ("post", "https://graph.facebook.com/v23.0/flow-new/publish"),
        ("post", _MESSAGES_URL),
    ]
    assert fake_redis.store[_flow_cache_key(schema_hash)] == "flow-new"


async def test_notify_form_tag_rides_the_token_as_the_fourth_segment(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A caller-set form_tag is appended as the token's 4th ``:``-delimited segment
    # (prefix:hash:nonce:tag); it rides the token only, never the Flow or the cache key.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    outcome = await WhatsAppChannel().notify(_form_notification(form_tag="ref-42"))

    assert outcome.message_ids == ["wamid.FLOW"]
    token = fake_httpx.calls[-1]["json"]["interactive"]["action"]["parameters"]["flow_token"]
    segments = token.split(":")
    assert len(segments) == 4  # prefix, hash, nonce, tag
    assert f"{segments[0]}:" == _NF_PREFIX
    assert segments[1] == schema_hash
    assert segments[3] == "ref-42"


async def test_notify_form_without_a_tag_mints_a_three_segment_token(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A tag-less notify-form token stays prefix:hash:nonce — no empty 4th segment.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    await WhatsAppChannel().notify(_form_notification())

    token = fake_httpx.calls[-1]["json"]["interactive"]["action"]["parameters"]["flow_token"]
    assert len(token.split(":")) == 3


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
        schema,
        [
            {"title": "Plan", "fields": ["tier"], "kind": "input", "display": []},
            {"title": "Say", "fields": ["note"], "kind": "input", "display": []},
        ],
        {"tier"},
    )
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-nf-prefill"
    fake_httpx.responses.append(_accepted("wamid.FLOW"))

    outcome = await WhatsAppChannel().notify(_form_notification(schema=schema, data=data, pages=pages))

    assert outcome.message_ids == ["wamid.FLOW"]
    params = fake_httpx.calls[0]["json"]["interactive"]["action"]["parameters"]
    assert params["flow_id"] == "flow-nf-prefill"
    assert params["flow_token"].startswith(f"{_NF_PREFIX}{schema_hash}:")
    action_payload = params["flow_action_payload"]
    assert action_payload["screen"] == "SCREEN_A"
    # The prefill value and the per-send option list reach the Flow's screen data model.
    assert action_payload["data"]["note__init"] == "hi"
    assert action_payload["data"]["tier__ds"] == [{"id": "g", "title": "Gold"}]


async def test_channel_advertises_form_notification_capability():
    assert WhatsAppChannel.supports_form_notifications is True


# --- Reacting form delivery (endpoint-driven Flow + key provisioning gate) ---------

_REACT_SCHEMA = {
    "type": "object",
    "properties": {"tier": {"type": "string", "enum": ["g", "s"]}, "note": {"type": "string"}},
    "required": ["tier"],
}
_FLOW_PRIVATE_PEM = None


def _private_pem() -> str:
    global _FLOW_PRIVATE_PEM
    if _FLOW_PRIVATE_PEM is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _FLOW_PRIVATE_PEM = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    return _FLOW_PRIVATE_PEM


@pytest.fixture
def reacting_env(waba_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", _private_pem())
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI", "https://app.example/api/channels/whatsapp/flow-data")
    reset_all_settings()


def _reacting_delivery(**overrides):
    from tai42_contract.interactions.models import FormReactions

    reactions = FormReactions(field_changed=["tier"], submitted=True, choices=["tier"])
    fields = {
        "answer_format": "form",
        "schema": _REACT_SCHEMA,
        "question": "Pick a tier.",
        "reactions": reactions,
    }
    fields.update(overrides)
    return make_delivery(**fields)


async def test_reacting_form_publishes_endpoint_driven_and_caches_its_sidecar(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    reactions = {"field_changed": ["tier"], "page_advanced": [], "submitted": True, "choices": ["tier"]}
    flow_json, _hash = build_form_flow(_REACT_SCHEMA, None, {"tier"}, reactions)
    assert flow_json["data_api_version"] == "3.0"

    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-react"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.R"))

    await WhatsAppChannel().deliver(_reacting_delivery())

    # The Flow is created endpoint-driven: the create carries the configured endpoint URI.
    create = fake_httpx.calls[0]["json"]
    assert create["endpoint_uri"] == "https://app.example/api/channels/whatsapp/flow-data"
    assert json.loads(create["flow_json"])["data_api_version"] == "3.0"
    # The reacting form's schema, reactions and option fields are cached under the interaction id.
    sidecar = json.loads(fake_redis.store["channel:whatsapp:endpoint-form:int-1"])
    assert sidecar["schema"] == _REACT_SCHEMA
    assert sidecar["reactions"] == reactions
    assert sidecar["option_fields"] == []
    # The pending record carries the reaction triggers so a re-send reproduces the same Flow.
    pending = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert pending["form_reactions"] == reactions


async def test_reacting_form_without_private_key_refuses_loudly(
    waba_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI", "https://app.example/api/channels/whatsapp/flow-data")
    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", raising=False)
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY"):
        await WhatsAppChannel().deliver(_reacting_delivery())
    # Refused before any network work and before any reservation.
    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_reacting_form_without_endpoint_uri_refuses_loudly(
    waba_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", _private_pem())
    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI", raising=False)
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI"):
        await WhatsAppChannel().deliver(_reacting_delivery())
    assert fake_httpx.calls == []


# --- shape-checked forms: endpoint-driven by their shape alone ---------------------------

_SHAPED_SCHEMA = {
    "type": "object",
    "properties": {
        "email": {"type": "string", "format": "email", "title": "Email address"},
        "on": {"type": "string", "format": "date"},
    },
    "required": ["email"],
}


def _shaped_delivery(**overrides):
    from tai42_contract.interactions.models import FieldWording, FormWording, Wording

    fields = {
        "answer_format": "form",
        "schema": _SHAPED_SCHEMA,
        "question": "Your address?",
        "wording": Wording(form=FormWording(fields={"email": FieldWording(invalid="Use your work address")})),
    }
    fields.update(overrides)
    return make_delivery(**fields)


async def test_a_shape_checked_ask_publishes_endpoint_driven_and_caches_its_sidecar(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-shaped"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.S"))

    await WhatsAppChannel().deliver(_shaped_delivery())

    create = fake_httpx.calls[0]["json"]
    assert create["endpoint_uri"] == "https://app.example/api/channels/whatsapp/flow-data"
    footer = json.loads(create["flow_json"])["screens"][0]["layout"]["children"][-1]
    assert footer["on-click-action"]["name"] == "data_exchange"
    sidecar = json.loads(fake_redis.store["channel:whatsapp:endpoint-form:int-1"])
    assert sidecar == {
        "schema": _SHAPED_SCHEMA,
        "pages": None,
        "reactions": None,
        "option_fields": [],
        "invalid": {"email": "Use your work address"},
        "required": {},
        "lines": {},
    }


async def test_a_shape_checked_ask_caches_the_callers_required_sentences_and_form_lines(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    from tai42_contract.interactions.models import FieldWording, FormWording, Wording

    wording = Wording(
        form=FormWording(
            error_line="Please check the form",
            update_failed="Not refreshed: {reason}",
            fields={"email": FieldWording(invalid="Use your work address", required="We need your email")},
        )
    )
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-shaped"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.S"))

    await WhatsAppChannel().deliver(_shaped_delivery(wording=wording))

    sidecar = json.loads(fake_redis.store["channel:whatsapp:endpoint-form:int-1"])
    assert sidecar["required"] == {"email": "We need your email"}
    assert sidecar["lines"] == {"error_line": "Please check the form", "update_failed": "Not refreshed: {reason}"}


async def test_an_ask_error_line_too_long_for_the_resent_body_is_refused_before_any_reservation(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormWording, Wording

    # The error line also fills the re-sent Flow's body in place of the door's reason, which is
    # bounded at 500 characters.
    wording = Wording(form=FormWording(error_line="x" * 501))
    with pytest.raises(ChannelInputError, match=r"wording\.form\.error_line is 501 characters"):
        await WhatsAppChannel().deliver(_shaped_delivery(wording=wording))
    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_an_ask_error_line_at_the_resent_body_bound_is_accepted(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormWording, Wording

    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-shaped"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.S"))
    await WhatsAppChannel().deliver(_shaped_delivery(wording=Wording(form=FormWording(error_line="x" * 500))))
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" in fake_redis.store


async def test_a_rejected_final_template_too_long_for_a_text_message_is_refused_at_deliver(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The closing sentence rides a text message (Meta caps a text body at 4096) with the door's line
    # (up to 500 characters) in each ``{reason}``: 1597 + 5 * 500 = 4097 is one over.
    over = "x" * 1597 + "{reason}" * 5
    with pytest.raises(ChannelInputError, match=r"wording\.answer\.rejected_final"):
        await WhatsAppChannel().deliver(_form_delivery(wording=Wording(answer=AnswerWording(rejected_final=over))))
    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_a_rejected_final_template_at_the_text_cap_is_accepted(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.store[_flow_cache_key(schema_hash)] = "flow-cached"
    fake_httpx.responses.append(_accepted("wamid.FORM"))
    at_cap = "x" * 1596 + "{reason}" * 5
    await WhatsAppChannel().deliver(_form_delivery(wording=Wording(answer=AnswerWording(rejected_final=at_cap))))
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" in fake_redis.store


async def test_a_shape_checked_notify_form_carries_its_lines_and_required_sentences(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    from tai42_contract.interactions.models import FieldWording, FormWording, Wording

    wording = Wording(
        form=FormWording(
            error_line="Please check the form",
            fields={"email": FieldWording(required="We need your email")},
        )
    )
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-nf"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.NF"))

    await WhatsAppChannel().notify(_form_notification(schema=_SHAPED_SCHEMA, wording=wording))

    flow = json.loads(fake_httpx.calls[0]["json"]["flow_json"])
    declared = flow["screens"][0]["data"]
    for key in ("tai42_error_line", "tai42_update_failed", "email__required"):
        assert declared[key]["type"] == "string"
    # Only a field the shape check can report missing carries a required sentence.
    assert "on__required" not in declared
    send = next(call["json"] for call in fake_httpx.calls if (call.get("json") or {}).get("type") == "interactive")
    data = send["interactive"]["action"]["parameters"]["flow_action_payload"]["data"]
    assert data["tai42_error_line"] == "Please check the form"
    assert data["tai42_update_failed"] == ""
    assert data["email__required"] == "We need your email"


async def test_an_ask_less_error_line_is_not_bound_by_the_resent_body(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_contract.interactions.models import FormWording, Wording

    # An ask-less form is never re-sent, so its error line takes no body bound.
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-nf"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.NF"))
    wording = Wording(form=FormWording(error_line="x" * 600))
    outcome = await WhatsAppChannel().notify(_form_notification(schema=_SHAPED_SCHEMA, wording=wording))
    assert outcome.message_ids == ["wamid.NF"]


@pytest.mark.parametrize("send", ["ask", "notify"])
async def test_a_required_sentence_over_its_controls_error_cap_is_refused_before_any_send(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx, send: str
):
    from tai42_contract.interactions.models import FieldWording, FormWording, Wording

    wording = Wording(form=FormWording(fields={"email": FieldWording(required="x" * 31)}))
    channel = WhatsAppChannel()
    sending = (
        channel.deliver(_shaped_delivery(wording=wording))
        if send == "ask"
        else channel.notify(_form_notification(schema=_SHAPED_SCHEMA, wording=wording))
    )
    with pytest.raises(ChannelInputError, match=r"wording\.form\.fields\['email'\]\.required is 31 characters"):
        await sending
    assert fake_httpx.calls == []


_BOOLEAN_SCHEMA = {
    "type": "object",
    "properties": {"agree": {"type": "boolean", "title": "Agree"}, "note": {"type": "string"}},
}
_BOOLEAN_PAGES = [
    {"title": "Ask", "fields": ["agree", "note"], "kind": "input", "display": []},
    {"title": "Check", "fields": [], "kind": "review", "display": []},
]


def _boolean_wording(true_label: str, false_label: str) -> Wording:
    from tai42_contract.interactions.models import FieldWording

    return Wording(form=FormWording(fields={"agree": FieldWording(true_label=true_label, false_label=false_label)}))


async def test_two_asks_that_differ_only_in_the_boolean_words_publish_two_flows(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    for flow_id in ("flow-si", "flow-oui"):
        fake_httpx.get_responses.append(flow_list_page())
        fake_httpx.responses.append(_flow_created(flow_id))
        fake_httpx.responses.append(_published())
        fake_httpx.responses.append(_accepted(f"wamid.{flow_id}"))
    await WhatsAppChannel().deliver(
        _form_delivery(schema=_BOOLEAN_SCHEMA, pages=_BOOLEAN_PAGES, wording=_boolean_wording("Si", "No"))
    )
    await WhatsAppChannel().deliver(
        _form_delivery(
            schema=_BOOLEAN_SCHEMA,
            pages=_BOOLEAN_PAGES,
            wording=_boolean_wording("Oui", "Non"),
            interaction_id="int-2",
            recipient=ALLOWED_B,
        )
    )
    creates = [call["json"] for call in fake_httpx.calls if (call.get("json") or {}).get("flow_json")]
    assert len(creates) == 2
    assert creates[0]["name"] != creates[1]["name"]
    assert "Agree: Si" in creates[0]["flow_json"]
    assert "Agree: Oui" in creates[1]["flow_json"]
    assert json.loads(creates[1]["flow_json"])["version"] == "7.0"


async def test_two_asks_with_the_same_boolean_words_reuse_one_flow(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-si"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.1"))
    fake_httpx.responses.append(_accepted("wamid.2"))
    await WhatsAppChannel().deliver(
        _form_delivery(schema=_BOOLEAN_SCHEMA, pages=_BOOLEAN_PAGES, wording=_boolean_wording("Si", "No"))
    )
    await WhatsAppChannel().deliver(
        _form_delivery(
            schema=_BOOLEAN_SCHEMA,
            pages=_BOOLEAN_PAGES,
            wording=_boolean_wording("Si", "No"),
            interaction_id="int-2",
            recipient=ALLOWED_B,
        )
    )
    creates = [call for call in fake_httpx.calls if (call.get("json") or {}).get("flow_json")]
    assert len(creates) == 1


async def test_a_bad_word_property_name_collides_with_a_form_line_carrier(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    schema = {
        **_SHAPED_SCHEMA,
        "properties": {**_SHAPED_SCHEMA["properties"], "tai42_update_failed": {"type": "string"}},
    }
    with pytest.raises(ChannelInputError, match="tai42_update_failed"):
        await WhatsAppChannel().deliver(_shaped_delivery(schema=schema))
    assert fake_httpx.calls == []


async def test_a_failed_sidecar_write_frees_the_pair_and_raises_the_store_error(
    reacting_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from redis.exceptions import RedisError

    real_set = fake_redis.set

    async def set_failing_on_the_sidecar(key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if key.startswith("channel:whatsapp:endpoint-form:"):
            raise RedisError("connection reset")
        return await real_set(key, value, nx=nx, ex=ex)

    monkeypatch.setattr(fake_redis, "set", set_failing_on_the_sidecar)

    with pytest.raises(RedisError, match="connection reset"):
        await WhatsAppChannel().deliver(_shaped_delivery(interaction_id="int-A"))
    # Nothing was sent, and neither the pair's hold nor a sidecar is left behind.
    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store
    assert "channel:whatsapp:endpoint-form:int-A" not in fake_redis.store

    # The pair is free: the next ask on it reserves and sends.
    monkeypatch.setattr(fake_redis, "set", real_set)
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-shaped"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.S"))
    await WhatsAppChannel().deliver(_shaped_delivery(interaction_id="int-B"))
    assert "channel:whatsapp:endpoint-form:int-B" in fake_redis.store


async def test_a_failed_send_after_the_sidecar_write_drops_the_sidecar_and_frees_the_pair(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-shaped"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(response(400, json={"error": {"code": 131009, "message": "Invalid recipient"}}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*131009"):
        await WhatsAppChannel().deliver(_shaped_delivery())
    assert ("redis_set", "channel:whatsapp:endpoint-form:int-1") in fake_redis.events
    assert "channel:whatsapp:endpoint-form:int-1" not in fake_redis.store
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_a_shape_checked_ask_without_key_material_refuses_before_any_reservation(
    waba_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI", "https://app.example/api/channels/whatsapp/flow-data")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY"):
        await WhatsAppChannel().deliver(_shaped_delivery())
    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


async def test_a_native_shapes_only_ask_stays_static_without_key_material(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    schema = {"type": "object", "properties": {"code": {"type": "string", "pattern": "^[0-9]{4}$"}}}
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-static"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.N"))

    await WhatsAppChannel().deliver(make_delivery(answer_format="form", schema=schema, question="Code?"))

    create = fake_httpx.calls[0]["json"]
    assert create.get("endpoint_uri") is None
    flow = json.loads(create["flow_json"])
    assert "data_api_version" not in flow
    assert flow["screens"][0]["layout"]["children"][0]["pattern"] == "^[0-9]{4}$"
    assert "channel:whatsapp:endpoint-form:int-1" not in fake_redis.store


@pytest.mark.parametrize(
    ("prop", "cap"),
    [({"type": "string", "format": "email"}, 30), ({"type": "string", "format": "date", "minDate": "2026-01-01"}, 80)],
)
async def test_a_callers_sentence_over_its_controls_error_cap_is_refused_before_any_reservation(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx, prop: dict, cap: int
):
    from tai42_contract.interactions.models import FieldWording, FormWording, Wording

    wording = Wording(form=FormWording(fields={"f": FieldWording(invalid="x" * (cap + 1))}))
    delivery = make_delivery(
        answer_format="form", schema={"type": "object", "properties": {"f": prop}}, question="?", wording=wording
    )
    with pytest.raises(ChannelInputError, match=f"caps the error message of a .* at {cap}"):
        await WhatsAppChannel().deliver(delivery)
    assert fake_httpx.calls == []
    assert f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}" not in fake_redis.store


# --- Flow resolve: lookup by name on a cache miss, adopt / clean up, one publisher ---------

_GRAPH = "https://graph.facebook.com/v23.0"
_FLOWS_URL = f"{_GRAPH}/{_WABA_ID}/flows"
_ENDPOINT_URI = "https://app.example/api/channels/whatsapp/flow-data"


def _identity_cache_key(schema_hash: str, endpoint_uri: str | None = None) -> str:
    return f"channel:whatsapp:flow:{_WABA_ID}:{expected_flow_identity(schema_hash, endpoint_uri)}"


def _pending_key(recipient: str = ALLOWED_A) -> str:
    return f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{recipient}"


async def test_form_cache_miss_lists_flows_before_creating(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [
        ("get", _FLOWS_URL),
        ("post", _FLOWS_URL),
        ("post", f"{_GRAPH}/flow-1/publish"),
        ("post", _MESSAGES_URL),
    ]
    assert fake_httpx.get_calls[0]["params"] is None  # the first page carries no cursor
    assert fake_httpx.get_calls[0]["headers"] == {"Authorization": "Bearer test-access-token"}
    assert fake_httpx.calls[0]["json"]["name"] == expected_flow_name(schema_hash)
    assert fake_redis.store[_identity_cache_key(schema_hash)] == "flow-1"


async def test_form_reuses_published_flow_found_by_name(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The cache is empty (the store was recreated) while the WABA already holds the Flow under
    # its name: the plugin adopts that Flow instead of creating a second one Meta would refuse.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(
        flow_list_page(
            listed_flow("flow-other", "tai42-form-someone-else", "PUBLISHED"),
            listed_flow("flow-live", expected_flow_name(schema_hash), "PUBLISHED"),
        )
    )
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL), ("post", _MESSAGES_URL)]  # no create, no publish
    assert fake_httpx.calls[0]["json"]["interactive"]["action"]["parameters"]["flow_id"] == "flow-live"
    assert fake_redis.store[_identity_cache_key(schema_hash)] == "flow-live"
    assert _identity_cache_key(schema_hash) not in fake_redis.ttls


@pytest.mark.parametrize("status", ["THROTTLED", "BLOCKED"])
async def test_form_adopts_throttled_and_blocked_flow(
    status: str, waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An unhealthy-endpoint state is still the plugin's published Flow; Meta lifts it by itself.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page(listed_flow("flow-held", expected_flow_name(schema_hash), status)))
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL), ("post", _MESSAGES_URL)]
    assert fake_httpx.calls[0]["json"]["interactive"]["action"]["parameters"]["flow_id"] == "flow-held"
    assert fake_redis.store[_identity_cache_key(schema_hash)] == "flow-held"


async def test_form_found_draft_is_deleted_then_recreated(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A draft under the name is an orphan whose content its name does not vouch for: it is
    # deleted, then a fresh Flow is created and published.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page(listed_flow("flow-draft", expected_flow_name(schema_hash), "DRAFT")))
    fake_httpx.responses.append(_published())  # the draft delete (its 2xx body is unused)
    fake_httpx.responses.append(_flow_created("flow-new"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [
        ("get", _FLOWS_URL),
        ("delete", f"{_GRAPH}/flow-draft"),
        ("post", _FLOWS_URL),
        ("post", f"{_GRAPH}/flow-new/publish"),
        ("post", _MESSAGES_URL),
    ]
    assert fake_httpx.calls[-1]["json"]["interactive"]["action"]["parameters"]["flow_id"] == "flow-new"
    assert fake_redis.store[_identity_cache_key(schema_hash)] == "flow-new"


async def test_form_found_draft_delete_failure_raises_without_create(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page(listed_flow("flow-draft", expected_flow_name(schema_hash), "DRAFT")))
    fake_httpx.responses.append(response(500, text="delete down"))

    with pytest.raises(ChannelDeliveryError, match="delete down"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL), ("delete", f"{_GRAPH}/flow-draft")]
    assert _identity_cache_key(schema_hash) not in fake_redis.store
    assert _pending_key() not in fake_redis.store


async def test_form_deprecated_match_is_logged_and_create_proceeds(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(
        flow_list_page(listed_flow("flow-retired", expected_flow_name(schema_hash), "DEPRECATED"))
    )
    fake_httpx.responses.append(_flow_created("flow-new"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    with caplog.at_level(logging.WARNING, logger="tai42_channel_whatsapp.channel.forms"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [
        ("get", _FLOWS_URL),
        ("post", _FLOWS_URL),
        ("post", f"{_GRAPH}/flow-new/publish"),
        ("post", _MESSAGES_URL),
    ]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "flow-retired" in warnings[0].getMessage()
    assert fake_redis.store[_identity_cache_key(schema_hash)] == "flow-new"


async def test_form_create_clash_after_lookup_propagates_meta_error(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The listing found nothing, yet the create clashes (a race, or a name Meta still reserves):
    # Meta's own refusal surfaces unchanged and is never interpreted into another path.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(
        response(
            400,
            json={
                "error": {
                    "message": "Invalid parameter",
                    "type": "OAuthException",
                    "code": 100,
                    "error_subcode": 4016019,
                    "error_user_title": "Flow name is not unique",
                    "error_user_msg": "Flow name should be unique within one WhatsApp Business Account. "
                    "Please select another name for your Flow.",
                }
            },
        )
    )

    with pytest.raises(ChannelDeliveryError, match="Flow name is not unique") as excinfo:
        await WhatsAppChannel().deliver(_form_delivery())

    assert excinfo.value.retryable is False
    assert _http_order(fake_httpx) == [("get", _FLOWS_URL), ("post", _FLOWS_URL)]  # no publish, no delete
    assert _identity_cache_key(schema_hash) not in fake_redis.store
    assert _pending_key() not in fake_redis.store


async def test_form_two_live_matches_raise_ambiguous(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    name = expected_flow_name(schema_hash)
    fake_httpx.get_responses.append(
        flow_list_page(listed_flow("flow-a", name, "PUBLISHED"), listed_flow("flow-b", name, "THROTTLED"))
    )

    with pytest.raises(ChannelDeliveryError, match=r"flow-a.*flow-b"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL)]  # no create
    assert _identity_cache_key(schema_hash) not in fake_redis.store
    assert _pending_key() not in fake_redis.store


async def test_form_unknown_flow_status_raises(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(
        flow_list_page(listed_flow("flow-odd", expected_flow_name(schema_hash), "ARCHIVED"))
    )

    with pytest.raises(ChannelDeliveryError, match=r"flow-odd.*ARCHIVED"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL)]
    assert _identity_cache_key(schema_hash) not in fake_redis.store


async def test_form_list_pagination_follows_after_cursor(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Pages are walked by the ``after`` cursor on the plugin's own URL (never the vendor's
    # ``next`` URL, which would carry the Bearer token wherever it points) until ``next`` is gone.
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(
        flow_list_page(listed_flow("flow-other", "tai42-form-other", "PUBLISHED"), after="c1", more=True)
    )
    fake_httpx.get_responses.append(
        flow_list_page(listed_flow("flow-live", expected_flow_name(schema_hash), "PUBLISHED"), after="c2")
    )
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery())

    assert [call["url"] for call in fake_httpx.get_calls] == [_FLOWS_URL, _FLOWS_URL]
    assert fake_httpx.get_calls[0]["params"] is None
    assert fake_httpx.get_calls[1]["params"] == {"after": "c1"}
    assert fake_httpx.calls[0]["json"]["interactive"]["action"]["parameters"]["flow_id"] == "flow-live"
    assert fake_redis.store[_identity_cache_key(schema_hash)] == "flow-live"


async def test_form_list_next_without_after_cursor_raises(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.get_responses.append(flow_list_page(more=True))

    with pytest.raises(ChannelDeliveryError, match="cursor"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL)]
    assert _pending_key() not in fake_redis.store


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(["not", "an", "object"], id="non-dict-body"),
        pytest.param({"data": {"id": "flow-1"}, "paging": {}}, id="data-not-a-list"),
        pytest.param({"data": [{"id": "flow-1", "name": "tai42-form-x"}], "paging": {}}, id="item-missing-status"),
        pytest.param({"data": [], "paging": ["next"]}, id="paging-not-an-object"),
    ],
)
async def test_form_unreadable_flow_list_raises(body, waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.get_responses.append(response(200, json=body))

    with pytest.raises(ChannelDeliveryError, match="flow list"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL)]  # no create
    assert _pending_key() not in fake_redis.store


async def test_reacting_form_identity_covers_endpoint_uri(
    reacting_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A published Flow's endpoint cannot be changed, so the endpoint is part of its identity:
    # the same reacting form under another data-endpoint URI is another Flow, by name and by key.
    from tai42_kit.settings import reset_all_settings

    reactions = {"field_changed": ["tier"], "page_advanced": [], "submitted": True, "choices": ["tier"]}
    _, react_hash = build_form_flow(_REACT_SCHEMA, None, set(), reactions)  # the delivery carries no per-send options
    other_uri = "https://other.example/api/channels/whatsapp/flow-data"
    for flow_id in ("flow-one", "flow-two"):
        fake_httpx.get_responses.append(flow_list_page())
        fake_httpx.responses.append(_flow_created(flow_id))
        fake_httpx.responses.append(_published())
        fake_httpx.responses.append(_accepted(f"wamid.{flow_id}"))

    await WhatsAppChannel().deliver(_reacting_delivery())
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI", other_uri)
    reset_all_settings()
    await WhatsAppChannel().deliver(_reacting_delivery(interaction_id="int-2", recipient=ALLOWED_B))

    creates = [call["json"] for call in fake_httpx.calls if call["url"] == _FLOWS_URL]
    assert [create["name"] for create in creates] == [
        expected_flow_name(react_hash, _ENDPOINT_URI),
        expected_flow_name(react_hash, other_uri),
    ]
    assert [create["endpoint_uri"] for create in creates] == [_ENDPOINT_URI, other_uri]
    assert fake_redis.store[_identity_cache_key(react_hash, _ENDPOINT_URI)] == "flow-one"
    assert fake_redis.store[_identity_cache_key(react_hash, other_uri)] == "flow-two"
    # The endpoint-less identity of the same hash is a third, distinct name.
    assert expected_flow_name(react_hash) not in {create["name"] for create in creates}


async def test_concurrent_first_sends_publish_one_flow(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Two first sends of one form race on a cold cache: the resolve lock lets exactly one list,
    # create and publish; the other re-reads the cache under the lock and reuses that Flow.
    import asyncio

    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.ONE"))
    fake_httpx.responses.append(_accepted("wamid.TWO"))

    await asyncio.gather(
        WhatsAppChannel().deliver(_form_delivery(interaction_id="int-1", recipient=ALLOWED_A)),
        WhatsAppChannel().deliver(_form_delivery(interaction_id="int-2", recipient=ALLOWED_B)),
    )

    assert _http_order(fake_httpx) == [
        ("get", _FLOWS_URL),
        ("post", _FLOWS_URL),
        ("post", f"{_GRAPH}/flow-1/publish"),
        ("post", _MESSAGES_URL),
        ("post", _MESSAGES_URL),
    ]
    sends = [call["json"] for call in fake_httpx.calls if call["url"] == _MESSAGES_URL]
    assert [send["interactive"]["action"]["parameters"]["flow_id"] for send in sends] == ["flow-1", "flow-1"]
    lock_key = f"channel:whatsapp:flow-lock:{_WABA_ID}:{expected_flow_identity(schema_hash)}"
    lock_events = [event for event in fake_redis.events if event[0] in ("redis_lock", "redis_unlock")]
    assert lock_events == [
        ("redis_lock", lock_key),
        ("redis_unlock", lock_key),
        ("redis_lock", lock_key),
        ("redis_unlock", lock_key),
    ]


async def test_resolve_lock_timeout_raises_retryable(
    waba_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Another send holds the resolve lock past the wait: a retryable delivery error, no Graph call.
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_HTTP_TIMEOUT_SECONDS", "0.01")
    reset_all_settings()
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    lock_key = f"channel:whatsapp:flow-lock:{_WABA_ID}:{expected_flow_identity(schema_hash)}"
    assert await fake_redis.lock(lock_key).acquire()  # held by the other send

    with pytest.raises(ChannelDeliveryError, match="publishing the WhatsApp Flow") as excinfo:
        await WhatsAppChannel().deliver(_form_delivery())

    assert excinfo.value.retryable is True
    assert fake_redis.locks[-1].timeout == pytest.approx(0.06)
    assert fake_redis.locks[-1].blocking_timeout == pytest.approx(0.06)
    assert _http_order(fake_httpx) == []
    assert _pending_key() not in fake_redis.store


async def test_resolve_lock_expired_before_release_logs_and_result_stands(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    _, schema_hash = build_form_flow(_FORM_SCHEMA)
    fake_redis.lock_expired = True
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    with caplog.at_level(logging.ERROR, logger="tai42_channel_whatsapp.correlation"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert fake_redis.store[_identity_cache_key(schema_hash)] == "flow-1"
    assert fake_httpx.calls[-1]["url"] == _MESSAGES_URL
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "expired before release" in errors[0].getMessage()


async def test_form_list_walk_past_the_page_bound_raises(
    waba_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The walk's end is the vendor's ``paging.next``; a list that never ends is a loud bound, not a hang.
    monkeypatch.setattr("tai42_channel_whatsapp.client._MAX_FLOW_LIST_PAGES", 2)
    fake_httpx.get_responses.append(flow_list_page(after="c1", more=True))
    fake_httpx.get_responses.append(flow_list_page(after="c2", more=True))

    with pytest.raises(ChannelDeliveryError, match="runs past 2 pages"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _http_order(fake_httpx) == [("get", _FLOWS_URL), ("get", _FLOWS_URL)]
    assert _pending_key() not in fake_redis.store


@pytest.mark.parametrize("step", ["acquire", "release"])
async def test_resolve_lock_failure_raises_delivery_error(
    step: str, waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A lock failure other than an expired release surfaces as the channel's own delivery error.
    from redis.exceptions import LockError

    setattr(fake_redis, f"lock_{step}_error", LockError("lock trouble"))
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())

    with pytest.raises(ChannelDeliveryError, match=f"{step[:-1]}ing the WhatsApp Flow resolve lock .*lock trouble"):
        await WhatsAppChannel().deliver(_form_delivery())

    assert _pending_key() not in fake_redis.store


async def test_a_shape_checked_notify_form_publishes_endpoint_driven_and_carries_its_sentences(
    reacting_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    import json

    from tai42_contract.interactions.models import FieldWording, FormWording, Wording

    wording = Wording(form=FormWording(fields={"email": FieldWording(invalid="Use your work address")}))
    fake_httpx.get_responses.append(flow_list_page())
    fake_httpx.responses.append(_flow_created("flow-nf"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.NF"))

    outcome = await WhatsAppChannel().notify(_form_notification(schema=_SHAPED_SCHEMA, wording=wording))

    assert outcome.message_ids == ["wamid.NF"]
    create = fake_httpx.calls[0]["json"]
    assert create["endpoint_uri"] == "https://app.example/api/channels/whatsapp/flow-data"
    flow = json.loads(create["flow_json"])
    assert flow["screens"][0]["data"]["email__invalid"] == {"type": "string", "__example__": "Not an accepted value"}
    send = next(call["json"] for call in fake_httpx.calls if (call.get("json") or {}).get("type") == "interactive")
    data = send["interactive"]["action"]["parameters"]["flow_action_payload"]["data"]
    assert data["email__invalid"] == "Use your work address"
    _, schema_hash = build_form_flow(
        _SHAPED_SCHEMA, field_invalid={"email": "Use your work address"}, carry_invalid=True
    )
    sidecar = json.loads(fake_redis.store[_schema_cache_key(schema_hash)])
    assert sidecar["pages"] is None
    assert sidecar["option_fields"] == []


async def test_a_shape_checked_notify_form_without_key_material_sends_nothing(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY"):
        await WhatsAppChannel().notify(
            _form_notification(
                schema=_SHAPED_SCHEMA,
                media=[MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Details page")],
            )
        )
    assert fake_httpx.calls == []
