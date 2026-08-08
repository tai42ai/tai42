"""``WhatsAppChannel.deliver`` and ``.notify`` — outbound JSON payload, tier
split, reservation ordering, the correlation-free notify path, sender_identity,
and the transient/hard classification a failed send raises."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelNotification, ChannelTemplate
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.client import (
    send_image,
    send_interactive_buttons,
    send_interactive_list,
    send_message,
    send_template,
)
from tai42_channel_whatsapp.correlation import PendingQuestionExistsError
from tests.conftest import ALLOWED_A, ALLOWED_B, PHONE_NUMBER_ID, FakeHttpx, FakeRedis, make_delivery, response

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_MESSAGES_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
_UNLISTED = "15559999999"


def _accepted(wamid: str = "wamid.OUT") -> httpx.Response:
    return response(200, json={"messages": [{"id": wamid}]})


def _known_contact_key(wa_id: str, phone_number_id: str = PHONE_NUMBER_ID) -> str:
    return f"channel:whatsapp:known-contact:{phone_number_id}:{wa_id}"


async def test_deliver_sends_exact_outbound_json(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery())

    assert len(fake_httpx.calls) == 1
    call = fake_httpx.calls[0]
    assert call["url"] == _MESSAGES_URL
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "text",
        "text": {"body": "Deploy to prod?"},
    }
    assert call["headers"] == {"Authorization": "Bearer test-access-token"}


async def test_messages_url_derives_from_api_base_url(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_API_BASE_URL", "http://127.0.0.1:9098/v23.0")
    reset_all_settings()
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery())

    assert fake_httpx.calls[0]["url"] == f"http://127.0.0.1:9098/v23.0/{PHONE_NUMBER_ID}/messages"


async def test_select_few_short_options_render_as_reply_buttons(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(
        make_delivery(answer_format="select", options=["staging", "production"], question="Which env?")
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "interactive"
    interactive = payload["interactive"]
    assert interactive["type"] == "button"
    assert interactive["body"] == {"text": "Which env?"}
    # ids bind the tap to this exact ask (interaction id "int-1"), index 0-based.
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "int-1:0", "title": "staging"}},
        {"type": "reply", "reply": {"id": "int-1:1", "title": "production"}},
    ]


async def test_select_many_options_render_as_interactive_list(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    options = [f"opt-{i}" for i in range(5)]  # >3 → past the button cap, within the list cap

    await WhatsAppChannel().deliver(make_delivery(answer_format="select", options=options, question="Pick one"))

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["body"] == {"text": "Pick one"}
    assert interactive["action"]["button"] == "Choose an option"
    rows = interactive["action"]["sections"][0]["rows"]
    assert rows == [{"id": f"int-1:{i}", "title": f"opt-{i}"} for i in range(5)]


async def test_select_long_option_title_forces_numbered_fallback(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    # An option longer than the list row-title cap (24) forces the numbered-text
    # fallback for the WHOLE ask — never a truncated title.
    options = ["short", "x" * 25]

    await WhatsAppChannel().deliver(make_delivery(answer_format="select", options=options, question="Which env?"))

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == f"Which env?\n1. short\n2. {'x' * 25}\nReply with the text of one option."


async def test_select_over_list_cap_count_forces_numbered_fallback(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    options = [f"o{i}" for i in range(11)]  # >10 rows → past the list cap

    await WhatsAppChannel().deliver(make_delivery(answer_format="select", options=options, question="Many?"))

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"].startswith("Many?\n1. o0")
    assert payload["text"]["body"].endswith("Reply with the text of one option.")


async def test_select_long_question_forces_numbered_fallback(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    # A question past the interactive body cap (1024) cannot ride an interactive
    # message body — the numbered-text fallback carries it.
    question = "q" * 1025

    await WhatsAppChannel().deliver(make_delivery(answer_format="select", options=["a", "b"], question=question))

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"].startswith(question)
    assert payload["text"]["body"].endswith("Reply with the text of one option.")


async def test_select_duplicate_titles_fall_from_buttons_to_list(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    # Reply-button titles must be unique; duplicate option text uses a list (whose
    # rows are keyed by unique id, not title) rather than a rejected button send.
    options = ["yes", "yes"]

    await WhatsAppChannel().deliver(make_delivery(answer_format="select", options=options, question="Sure?"))

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"]["sections"][0]["rows"] == [
        {"id": "int-1:0", "title": "yes"},
        {"id": "int-1:1", "title": "yes"},
    ]


async def test_select_reservation_carries_options_and_interaction_id(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    import json

    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(
        make_delivery(answer_format="select", options=["staging", "production"], question="Which env?")
    )

    stored = json.loads(fake_redis.store[f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_A}"])
    assert stored["options"] == ["staging", "production"]
    assert stored["interaction_id"] == "int-1"


async def test_plain_text_ask_sends_body_only(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery(answer_format="text", question="Deploy to prod?"))

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == "Deploy to prod?"


async def test_reserve_precedes_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery())

    kinds = [kind for kind, _ in fake_redis.events]
    assert kinds.index("redis_set") < kinds.index("http_post")


async def test_second_concurrent_question_rejected_loudly(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery())

    with pytest.raises(PendingQuestionExistsError) as excinfo:
        await WhatsAppChannel().deliver(make_delivery(interaction_id="int-2"))

    assert isinstance(excinfo.value, ChannelDeliveryError)
    assert len(fake_httpx.calls) == 1  # no second send happened


async def test_send_rejection_raises_and_releases_reservation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(400, json={"error": {"code": 131009, "message": "Invalid recipient"}}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*131009"):
        await WhatsAppChannel().deliver(make_delivery())

    # The pair was released — a follow-up deliver succeeds.
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery())


async def test_send_rejection_detail_bounded_for_non_json_body(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(502, text="<html>" + "x" * 2000 + "</html>"))

    with pytest.raises(ChannelDeliveryError, match="HTTP 502") as excinfo:
        await WhatsAppChannel().deliver(make_delivery())

    assert len(str(excinfo.value)) < 600  # detail capped at 500 chars


async def test_transport_failure_raises_and_releases_reservation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(httpx.ConnectError("boom"))

    with pytest.raises(ChannelDeliveryError, match="transport") as excinfo:
        await WhatsAppChannel().deliver(make_delivery())

    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery())


async def test_allowlisted_recipient_sends_and_correlates_to_it(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery(recipient=ALLOWED_B))

    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_B
    # The reservation is keyed per recipient — the pair uses the actual target.
    assert list(fake_redis.store) == [f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_B}"]


async def test_no_recipient_rejected_no_default(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # This channel has no operator default recipient — a recipientless ask is refused.
    with pytest.raises(ChannelDeliveryError, match="no default recipient"):
        await WhatsAppChannel().deliver(make_delivery(recipient=None))

    assert not fake_httpx.calls
    assert not fake_redis.store


async def test_freeform_unlisted_recipient_sends_and_correlates(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A question is a FREEFORM send: freeform delivery to an unlisted recipient is
    # not fenced by the allowlist — Meta's own 24-hour window is the fence. An
    # unlisted recipient sends and reserves.
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery(recipient=_UNLISTED))

    assert fake_httpx.calls[0]["json"]["to"] == _UNLISTED
    assert list(fake_redis.store) == [f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{_UNLISTED}"]


async def test_template_allowlist_padded_entry_matches_unpadded_recipient(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    # Allowlist-entry normalization (fail-open) stays meaningful only on the
    # template path — a padded JSON entry matches the unpadded recipient there.
    monkeypatch.setenv("CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS", '[" 15551230001 ", ""]')
    reset_all_settings()

    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Your order shipped.",
            recipient=ALLOWED_A,
            template=ChannelTemplate(name="order_update", language="en_US"),
        )
    )

    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_A
    assert fake_httpx.calls[0]["json"]["type"] == "template"


async def test_empty_allowlist_freeform_sends_but_cold_template_rejected(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS")
    reset_all_settings()

    # Freeform to any recipient succeeds even with an empty allowlist.
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery(recipient=ALLOWED_A))
    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_A

    # The fail-closed policy moved to templates: a cold unlisted number is refused.
    with pytest.raises(ChannelDeliveryError, match=r"template send to .* refused"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Cold ping.",
                recipient=ALLOWED_A,
                template=ChannelTemplate(name="ping", language="en_US"),
            )
        )
    assert len(fake_httpx.calls) == 1  # only the freeform send happened


async def test_deliver_missing_phone_number_id_raises_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"):
        await WhatsAppChannel().deliver(make_delivery())

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store  # nothing reserved
    assert not fake_redis.events  # no HTTP call and no Redis write at all


async def test_deliver_missing_access_token_raises_and_releases_reservation(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_ACCESS_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_ACCESS_TOKEN"):
        await WhatsAppChannel().deliver(make_delivery())

    assert not fake_httpx.calls  # checked before any network work — nothing sent
    assert not fake_redis.store  # the Tier-2 reservation was released, the pair is free


async def test_past_deadline_raises_before_any_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await WhatsAppChannel().deliver(make_delivery(timeout_at=datetime.now(UTC) - timedelta(seconds=1)))

    assert not fake_redis.store
    assert not fake_httpx.calls


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_past_deadline_raises_before_any_send(
    answer_format: str, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await WhatsAppChannel().deliver(
            make_delivery(answer_format=answer_format, timeout_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert not fake_httpx.calls  # the link would be dead on arrival — nothing sent
    assert not fake_redis.store
    assert not fake_redis.events  # no HTTP call and no reservation attempt at all


async def test_accepted_response_without_id_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(200, json={"messages": [{}]}))

    with pytest.raises(ChannelDeliveryError, match="no message id"):
        await WhatsAppChannel().deliver(make_delivery())


async def test_accepted_response_empty_messages_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(200, json={"messages": []}))

    with pytest.raises(ChannelDeliveryError, match="no message id"):
        await WhatsAppChannel().deliver(make_delivery())


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_link_send_skips_reservation(answer_format: str, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    delivery = make_delivery(answer_format=answer_format)
    await WhatsAppChannel().deliver(delivery)

    assert len(fake_httpx.calls) == 1
    body = fake_httpx.calls[0]["json"]["text"]["body"]
    assert delivery.question in body
    assert delivery.callback_url in body  # plain tappable link
    assert not fake_redis.store  # no reservation, no correlation
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]


async def test_notify_sends_plain_body_without_correlation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.N"))

    ids = await WhatsAppChannel().notify(ChannelNotification(message="Deploy finished.", recipient=ALLOWED_A))

    assert ids == ["wamid.N"]
    call = fake_httpx.calls[0]
    assert call["url"] == _MESSAGES_URL
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "text",
        "text": {"body": "Deploy finished."},
    }
    # Fire-and-forget: no reservation, no Redis write of any kind.
    assert not fake_redis.store
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]


async def test_notify_returns_wamid(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.RET"))

    ids = await WhatsAppChannel().notify(ChannelNotification(message="Done.", recipient=ALLOWED_A))

    assert ids == ["wamid.RET"]


async def test_notify_freeform_unlisted_recipient_sends(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A freeform notify is unfenced by the allowlist (Meta's window is the fence):
    # an unlisted recipient sends.
    fake_httpx.responses.append(_accepted("wamid.FREE"))

    ids = await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient=_UNLISTED))

    assert ids == ["wamid.FREE"]
    assert fake_httpx.calls[0]["json"]["to"] == _UNLISTED
    assert not fake_redis.store  # notify never touches the correlation store


async def test_notify_no_recipient_and_no_sender_identity_rejected(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="no default recipient"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up."))

    assert not fake_httpx.calls


async def test_notify_missing_phone_number_id_raises_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient=ALLOWED_A))

    assert not fake_httpx.calls
    assert not fake_redis.store


async def test_notify_missing_access_token_raises_and_nothing_sent(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_ACCESS_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_ACCESS_TOKEN"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient=ALLOWED_A))

    assert not fake_httpx.calls  # checked before any network work — nothing sent
    assert not fake_redis.store  # notify never touches the correlation store


async def test_notify_sender_identity_sends_from_it_and_bypasses_allowlist(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # sender_identity present: send FROM that phone_number_id to the recipient
    # VERBATIM, the recipient allowlist NOT consulted (recipient is off-allowlist).
    fake_httpx.responses.append(_accepted("wamid.BRIDGE"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(message="Reply.", recipient="15559999999", sender_identity="20000000000009")
    )

    assert ids == ["wamid.BRIDGE"]
    call = fake_httpx.calls[0]
    assert call["url"] == "https://graph.facebook.com/v23.0/20000000000009/messages"  # sent from the routed identity
    assert call["json"]["to"] == "15559999999"  # delivered to the initiator despite not being allowlisted
    assert not fake_redis.store  # fire-and-forget, no correlation


async def test_notify_no_sender_identity_uses_default_freeform_unfenced(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # sender_identity absent: the configured default phone_number_id sends, and a
    # FREEFORM notify to an unlisted recipient is not fenced by the allowlist.
    fake_httpx.responses.append(_accepted("wamid.UNL"))
    ids = await WhatsAppChannel().notify(ChannelNotification(message="Reply.", recipient=_UNLISTED))
    assert ids == ["wamid.UNL"]
    assert fake_httpx.calls[0]["url"] == _MESSAGES_URL  # the default phone_number_id
    assert fake_httpx.calls[0]["json"]["to"] == _UNLISTED

    fake_httpx.responses.append(_accepted("wamid.DEF"))
    ids = await WhatsAppChannel().notify(ChannelNotification(message="Reply.", recipient=ALLOWED_A))
    assert ids == ["wamid.DEF"]
    assert fake_httpx.calls[1]["json"]["to"] == ALLOWED_A


async def test_notify_sender_identity_without_recipient_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A bridge reply always carries the initiator's wa_id; with none there is no
    # target and no operator default — refuse loudly.
    with pytest.raises(ChannelDeliveryError, match="requires a recipient"):
        await WhatsAppChannel().notify(ChannelNotification(message="Reply.", sender_identity="20000000000009"))

    assert not fake_httpx.calls


async def test_notify_two_phone_number_ids_each_send_from_their_own(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # one credential, many numbers — each sender_identity replies from its own number.
    fake_httpx.responses.append(_accepted("wamid.A"))
    fake_httpx.responses.append(_accepted("wamid.B"))

    await WhatsAppChannel().notify(
        ChannelNotification(message="From A.", recipient="15551110000", sender_identity="30000000000001")
    )
    await WhatsAppChannel().notify(
        ChannelNotification(message="From B.", recipient="15552220000", sender_identity="30000000000002")
    )

    assert fake_httpx.calls[0]["url"] == "https://graph.facebook.com/v23.0/30000000000001/messages"
    assert fake_httpx.calls[0]["json"]["to"] == "15551110000"
    assert fake_httpx.calls[1]["url"] == "https://graph.facebook.com/v23.0/30000000000002/messages"
    assert fake_httpx.calls[1]["json"]["to"] == "15552220000"


async def test_notify_rejection_raises_delivery_error(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A synchronous non-2xx (e.g. an invalid recipient) surfaces as the loud error;
    # the 24h-window failure is usually async — see the status-webhook test path.
    fake_httpx.responses.append(response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*131047"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient=ALLOWED_A))

    assert not fake_redis.store  # notify never touches the correlation store


# --- Payload builders (wire shape) --------------------------------------------


async def test_send_image_builder_with_and_without_caption(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_image(PHONE_NUMBER_ID, ALLOWED_A, "https://cdn.example/a.jpg", "Front view")
    assert fake_httpx.calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "image",
        "image": {"link": "https://cdn.example/a.jpg", "caption": "Front view"},
    }

    fake_httpx.responses.append(_accepted())
    await send_image(PHONE_NUMBER_ID, ALLOWED_A, "https://cdn.example/b.jpg", None)
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/b.jpg"}  # no caption key


async def test_send_template_builder_with_and_without_parameters(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_template(
        PHONE_NUMBER_ID,
        ALLOWED_A,
        ChannelTemplate(name="order_update", language="en_US", parameters=["Jane", "SHIP-1"]),
    )
    assert fake_httpx.calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "template",
        "template": {
            "name": "order_update",
            "language": {"code": "en_US"},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": "Jane"}, {"type": "text", "text": "SHIP-1"}]}
            ],
        },
    }

    fake_httpx.responses.append(_accepted())
    await send_template(PHONE_NUMBER_ID, ALLOWED_A, ChannelTemplate(name="hello", language="en_US"))
    assert "components" not in fake_httpx.calls[1]["json"]["template"]  # no params → no components


async def test_send_interactive_buttons_builder_shape(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_interactive_buttons(PHONE_NUMBER_ID, ALLOWED_A, "Pick", [("id0", "A"), ("id1", "B")])
    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "button"
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "id0", "title": "A"}},
        {"type": "reply", "reply": {"id": "id1", "title": "B"}},
    ]


async def test_send_interactive_list_builder_shape(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_interactive_list(PHONE_NUMBER_ID, ALLOWED_A, "Pick", "Choose an option", [("id0", "A"), ("id1", "B")])
    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"] == {
        "button": "Choose an option",
        "sections": [{"rows": [{"id": "id0", "title": "A"}, {"id": "id1", "title": "B"}]}],
    }


# --- Notify: media and template -----------------------------------------------


async def test_notify_media_sends_body_with_links_then_each_image(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.BODY"))
    fake_httpx.responses.append(_accepted("wamid.IMG1"))
    fake_httpx.responses.append(_accepted("wamid.IMG2"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Here are the photos.",
            recipient=ALLOWED_A,
            media=[
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg", caption="Front"),
                MediaItem(kind=MediaKind.LINK, url="https://shop.example/p/1", caption="Product page"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/b.jpg"),
            ],
        )
    )

    assert ids == ["wamid.BODY", "wamid.IMG1", "wamid.IMG2"]  # every wamid, in send order
    # Body first, with the link item appended as a text line.
    assert fake_httpx.calls[0]["json"]["type"] == "text"
    assert fake_httpx.calls[0]["json"]["text"]["body"] == "Here are the photos.\nProduct page: https://shop.example/p/1"
    # Then each image as its own image message, in order.
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/a.jpg", "caption": "Front"}
    assert fake_httpx.calls[2]["json"]["image"] == {"link": "https://cdn.example/b.jpg"}


async def test_notify_media_data_image_refused_before_any_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # WhatsApp's link-sourced image send takes a public URL; a data: image is
    # refused loudly BEFORE anything is sent, naming the constraint.
    with pytest.raises(ChannelDeliveryError, match="data: image URL"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Photo.",
                recipient=ALLOWED_A,
                media=[MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAABBBB")],
            )
        )

    assert not fake_httpx.calls  # nothing sent — not even the body


async def test_notify_partial_media_send_names_already_sent_wamids(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A multi-part send that fails on the Nth part raises naming the wamids already
    # delivered — partial delivery stays visible.
    fake_httpx.responses.append(_accepted("wamid.BODY"))
    fake_httpx.responses.append(_accepted("wamid.IMG1"))
    fake_httpx.responses.append(response(400, json={"error": {"code": 131053, "message": "media download error"}}))

    with pytest.raises(ChannelDeliveryError, match="after delivering") as excinfo:
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Photos.",
                recipient=ALLOWED_A,
                media=[
                    MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/ok.jpg"),
                    MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/bad.jpg"),
                ],
            )
        )

    assert "wamid.BODY" in str(excinfo.value)
    assert "wamid.IMG1" in str(excinfo.value)
    assert len(fake_httpx.calls) == 3  # body, first image, failed second image


async def test_notify_template_maps_body_parameters(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.TPL"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Order shipped.",
            recipient=ALLOWED_A,
            template=ChannelTemplate(name="order_update", language="en_US", parameters=["Jane", "SHIP-1"]),
        )
    )

    assert ids == ["wamid.TPL"]
    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "template"
    assert payload["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Jane"}, {"type": "text", "text": "SHIP-1"}]}
    ]


# --- Recipient policy (template fence) ----------------------------------------


async def test_template_to_known_contact_sends(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An unlisted recipient the webhook saw within the window is a known contact —
    # a template send reaches them without an allowlist entry.
    fake_redis.store[_known_contact_key(_UNLISTED)] = "1"
    fake_httpx.responses.append(_accepted("wamid.KC"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Hi again.",
            recipient=_UNLISTED,
            template=ChannelTemplate(name="reengage", language="en_US"),
        )
    )

    assert ids == ["wamid.KC"]
    assert fake_httpx.calls[0]["json"]["to"] == _UNLISTED


async def test_template_to_cold_unlisted_recipient_rejected(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A cold, unlisted number (no known-contact marker) is refused loudly.
    with pytest.raises(ChannelDeliveryError, match=r"template send to .* refused"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Cold.",
                recipient=_UNLISTED,
                template=ChannelTemplate(name="reengage", language="en_US"),
            )
        )

    assert not fake_httpx.calls


async def test_template_known_contact_keys_on_send_from_number(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The known-contact lookup keys on the RESOLVED send-from phone_number_id: a
    # marker under a DIFFERENT number does not admit a template send.
    fake_redis.store[_known_contact_key(_UNLISTED, phone_number_id="99999999999999")] = "1"

    with pytest.raises(ChannelDeliveryError, match=r"template send to .* refused"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Cold.",
                recipient=_UNLISTED,
                template=ChannelTemplate(name="reengage", language="en_US"),
            )
        )

    assert not fake_httpx.calls


async def test_channel_advertises_media_and_template_capabilities():
    assert WhatsAppChannel.supports_media_notifications is True
    assert WhatsAppChannel.supports_template_notifications is True


# --- Send-failure classification (the caller's retry decision) ----------------


@pytest.mark.parametrize(
    ("queued", "retryable", "retry_after"),
    [
        pytest.param(httpx.ConnectError("boom"), True, None, id="transport"),
        pytest.param(response(500, json={"error": {"code": 1}}), True, None, id="500"),
        pytest.param(response(503, text="<html>down</html>"), True, None, id="503-html"),
        pytest.param(
            response(503, text="<html>down</html>", headers={"Retry-After": "7"}), True, 7.0, id="503-seconds"
        ),
        pytest.param(
            response(429, json={"error": {"code": 130429}}, headers={"Retry-After": "7"}), True, 7.0, id="429-seconds"
        ),
        pytest.param(
            response(429, json={"error": {"code": 130429}}, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            True,
            None,
            id="429-http-date",
        ),
        pytest.param(response(429, json={"error": {"code": 130429}}), True, None, id="429-no-header"),
        pytest.param(response(400, json={"error": {"code": 80007, "message": "rate limit"}}), True, None, id="80007"),
        pytest.param(
            response(400, json={"error": {"code": 131030, "message": "not in allowed list"}}), False, None, id="131030"
        ),
        pytest.param(
            response(400, json={"error": {"code": 131047, "message": "re-engagement"}}), False, None, id="131047"
        ),
        pytest.param(response(401, json={"error": {"code": 190, "message": "expired"}}), False, None, id="401"),
        pytest.param(response(400, text="<html>nope</html>"), False, None, id="unparseable-4xx"),
        pytest.param(response(200, json={"messages": []}), False, None, id="accepted-without-id"),
    ],
)
async def test_send_failure_carries_its_retry_classification(
    queued: httpx.Response | Exception, retryable: bool, retry_after: float | None, fake_httpx: FakeHttpx
):
    # The send seam classifies once; the central caller owns the retry loop.
    fake_httpx.responses.append(queued)

    with pytest.raises(ChannelDeliveryError) as excinfo:
        await send_message(PHONE_NUMBER_ID, ALLOWED_A, "hi")

    assert excinfo.value.retryable is retryable
    assert excinfo.value.retry_after == retry_after


async def test_non_dict_json_body_uses_raw_text_and_stays_non_retryable(fake_httpx: FakeHttpx):
    # A body that is valid JSON but NOT an object (a bare string or array) has no
    # ``error`` map — the raw response text carries the detail (never a bare
    # code=None message=None), and a 400 without a rate-limit code stays non-retryable.
    fake_httpx.responses.append(response(400, json=["oops", 1]))

    with pytest.raises(ChannelDeliveryError, match="HTTP 400") as excinfo:
        await send_message(PHONE_NUMBER_ID, ALLOWED_A, "hi")

    assert "oops" in str(excinfo.value)  # the raw JSON body text
    assert "code=None" not in str(excinfo.value)
    assert excinfo.value.retryable is False


async def test_non_dict_error_value_uses_raw_text_and_stays_non_retryable(fake_httpx: FakeHttpx):
    # An object body whose ``error`` is NOT a map (a bare string or null) has no
    # ``error.code``/``error.message`` to read — the raw response text carries the
    # detail (a ``ChannelDeliveryError``, never an AttributeError leaking out of
    # ``_error_detail``), and a 400 without a rate-limit code stays non-retryable.
    fake_httpx.responses.append(response(400, json={"error": "forbidden"}))

    with pytest.raises(ChannelDeliveryError, match="HTTP 400") as excinfo:
        await send_message(PHONE_NUMBER_ID, ALLOWED_A, "hi")

    assert "forbidden" in str(excinfo.value)  # the raw JSON body text
    assert "code=None" not in str(excinfo.value)
    assert excinfo.value.retryable is False


async def test_missing_access_token_is_not_retryable(fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    # A config fault: no fresh attempt can fix it.
    monkeypatch.delenv("CHANNEL_WHATSAPP_ACCESS_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError) as excinfo:
        await send_message(PHONE_NUMBER_ID, ALLOWED_A, "hi")

    assert excinfo.value.retryable is False
    assert not fake_httpx.calls
