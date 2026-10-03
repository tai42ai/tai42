"""Outbound payload builders (wire shape) — image/document/video/audio/location,
templates, the interactive button/list/cta_url shapes, the read receipt, and the
read+typing signal."""

from __future__ import annotations

import pytest
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelInputError,
    ChannelTemplate,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp import WhatsAppChannel
from tai42_channel_whatsapp.channel.template_params import TEMPLATE_BUTTONS_MAX
from tai42_channel_whatsapp.client import (
    mark_read,
    mark_read_typing,
    send_audio,
    send_document,
    send_image,
    send_interactive_buttons,
    send_interactive_cta_url,
    send_interactive_list,
    send_location,
    send_template,
    send_video,
)

from .conftest import _MESSAGES_URL, ALLOWED_A, PHONE_NUMBER_ID, FakeHttpx, FakeRedis, _accepted, response

pytestmark = pytest.mark.usefixtures("whatsapp_env")


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
        ChannelTemplate(name="status_update", language="en_US", parameters={"body_parameters": ["Jane", "A-42"]}),
    )
    assert fake_httpx.calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "template",
        "template": {
            "name": "status_update",
            "language": {"code": "en_US"},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": "Jane"}, {"type": "text", "text": "A-42"}]}
            ],
        },
    }

    fake_httpx.responses.append(_accepted())
    await send_template(PHONE_NUMBER_ID, ALLOWED_A, ChannelTemplate(name="hello", language="en_US"))
    assert "components" not in fake_httpx.calls[1]["json"]["template"]  # no runtime args → no components


async def test_send_template_builder_maps_header_media_and_buttons(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The template's NAMED components map onto the Cloud API template-message components array:
    # a header component for header_media, a body component for body_parameters, and one button
    # component per buttons entry (quick-reply payload / url text suffix, positional by index).
    fake_httpx.responses.append(_accepted())
    await send_template(
        PHONE_NUMBER_ID,
        ALLOWED_A,
        ChannelTemplate(
            name="status_update",
            language="en_US",
            parameters={
                "header_media": MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/banner.jpg"),
                "body_parameters": ["Jane", "A-42"],
                "buttons": [{"kind": "quick_reply", "payload": "STOP"}, {"kind": "url", "url_parameter": "item/42"}],
            },
        ),
    )
    assert fake_httpx.calls[0]["json"]["template"]["components"] == [
        {"type": "header", "parameters": [{"type": "image", "image": {"link": "https://cdn.example/banner.jpg"}}]},
        {"type": "body", "parameters": [{"type": "text", "text": "Jane"}, {"type": "text", "text": "A-42"}]},
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "0",
            "parameters": [{"type": "payload", "payload": "STOP"}],
        },
        {"type": "button", "sub_type": "url", "index": "1", "parameters": [{"type": "text", "text": "item/42"}]},
    ]


async def test_send_template_document_header_carries_filename(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_template(
        PHONE_NUMBER_ID,
        ALLOWED_A,
        ChannelTemplate(
            name="report",
            language="en_US",
            parameters={
                "header_media": MediaItem(
                    kind=MediaKind.DOCUMENT, url="https://cdn.example/report.pdf", filename="report.pdf"
                )
            },
        ),
    )
    assert fake_httpx.calls[0]["json"]["template"]["components"] == [
        {
            "type": "header",
            "parameters": [
                {"type": "document", "document": {"link": "https://cdn.example/report.pdf", "filename": "report.pdf"}}
            ],
        }
    ]


async def test_send_template_audio_header_refused(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A template header cannot carry audio (no Cloud API representation) — a permanent refusal.
    with pytest.raises(ChannelInputError, match="template header cannot carry audio"):
        await send_template(
            PHONE_NUMBER_ID,
            ALLOWED_A,
            ChannelTemplate(
                name="jingle",
                language="en_US",
                parameters={"header_media": MediaItem(kind=MediaKind.AUDIO, url="https://cdn.example/a.mp3")},
            ),
        )
    assert not fake_httpx.calls


def test_validate_template_accepts_this_channels_parameter_shape():
    # This channel owns the opaque ``parameters`` shape; a well-formed one passes its hook.
    WhatsAppChannel().validate_template(
        ChannelTemplate(
            name="status_update",
            language="en_US",
            parameters={
                "header_media": {"kind": "image", "url": "https://cdn.example/banner.jpg"},
                "body_parameters": ["Jane", "A-42"],
                "buttons": [{"kind": "quick_reply", "payload": "STOP"}],
            },
        )
    )


def test_validate_template_rejects_an_unknown_parameter_key():
    # A parameter shape this channel could not map (an unknown key) is refused loudly up front.
    with pytest.raises(ValueError, match="invalid WhatsApp template parameters"):
        WhatsAppChannel().validate_template(
            ChannelTemplate(name="t", language="en_US", parameters={"not_a_whatsapp_field": 1})
        )


def test_validate_template_rejects_an_over_cap_button_list():
    buttons = [{"kind": "quick_reply", "payload": f"p{i}"} for i in range(TEMPLATE_BUTTONS_MAX + 1)]
    with pytest.raises(ValueError, match="invalid WhatsApp template parameters"):
        WhatsAppChannel().validate_template(
            ChannelTemplate(name="t", language="en_US", parameters={"buttons": buttons})
        )


def test_validate_template_rejects_a_link_header():
    with pytest.raises(ValueError, match="invalid WhatsApp template parameters"):
        WhatsAppChannel().validate_template(
            ChannelTemplate(
                name="t",
                language="en_US",
                parameters={"header_media": {"kind": "link", "url": "https://x.example/p"}},
            )
        )


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
    await send_interactive_list(
        PHONE_NUMBER_ID,
        ALLOWED_A,
        "Pick",
        "Choose an option",
        [{"rows": [{"id": "id0", "title": "A"}, {"id": "id1", "title": "B"}]}],
    )
    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"] == {
        "button": "Choose an option",
        "sections": [{"rows": [{"id": "id0", "title": "A"}, {"id": "id1", "title": "B"}]}],
    }


async def test_send_interactive_list_multi_section_with_descriptions_and_header_footer(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_httpx.responses.append(_accepted())
    await send_interactive_list(
        PHONE_NUMBER_ID,
        ALLOWED_A,
        "Pick a dish",
        "Menu",
        [
            {"title": "Starters", "rows": [{"id": "s0", "title": "Soup", "description": "Tomato basil"}]},
            {"title": "Mains", "rows": [{"id": "m0", "title": "Steak"}]},
        ],
        header={"type": "image", "image": {"link": "https://cdn.example/menu.jpg"}},
        footer="Prices include tax",
    )
    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["header"] == {"type": "image", "image": {"link": "https://cdn.example/menu.jpg"}}
    assert interactive["footer"] == {"text": "Prices include tax"}
    assert interactive["action"]["sections"] == [
        {"title": "Starters", "rows": [{"id": "s0", "title": "Soup", "description": "Tomato basil"}]},
        {"title": "Mains", "rows": [{"id": "m0", "title": "Steak"}]},
    ]


async def test_send_interactive_cta_url_builder_shape(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_interactive_cta_url(PHONE_NUMBER_ID, ALLOWED_A, "Pay now", "Open portal", "https://pay.example/42")
    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "cta_url"
    assert interactive["body"] == {"text": "Pay now"}
    assert interactive["action"] == {
        "name": "cta_url",
        "parameters": {"display_text": "Open portal", "url": "https://pay.example/42"},
    }


async def test_send_interactive_buttons_header_footer_added_only_when_set(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_interactive_buttons(
        PHONE_NUMBER_ID,
        ALLOWED_A,
        "Pick",
        [("id0", "A")],
        header={"type": "image", "image": {"link": "https://cdn.example/h.jpg"}},
        footer="footer line",
    )
    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["header"] == {"type": "image", "image": {"link": "https://cdn.example/h.jpg"}}
    assert interactive["footer"] == {"text": "footer line"}


async def test_send_document_video_audio_location_builders(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await send_document(PHONE_NUMBER_ID, ALLOWED_A, "https://cdn.example/r.pdf", "Q3 report", "report.pdf")
    assert fake_httpx.calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "document",
        "document": {"link": "https://cdn.example/r.pdf", "caption": "Q3 report", "filename": "report.pdf"},
    }

    fake_httpx.responses.append(_accepted())
    await send_video(PHONE_NUMBER_ID, ALLOWED_A, "https://cdn.example/clip.mp4", None)
    assert fake_httpx.calls[1]["json"]["video"] == {"link": "https://cdn.example/clip.mp4"}  # no caption key

    fake_httpx.responses.append(_accepted())
    await send_audio(PHONE_NUMBER_ID, ALLOWED_A, "https://cdn.example/a.mp3")
    assert fake_httpx.calls[2]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "audio",
        "audio": {"link": "https://cdn.example/a.mp3"},  # no caption/filename on audio
    }

    fake_httpx.responses.append(_accepted())
    await send_location(PHONE_NUMBER_ID, ALLOWED_A, 51.5, -0.12, "Office", "1 High St")
    assert fake_httpx.calls[3]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "location",
        "location": {"latitude": 51.5, "longitude": -0.12, "name": "Office", "address": "1 High St"},
    }


async def test_mark_read_rides_send_and_posts_read_only_body(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The read receipt answers {"success": true} with NO messages[].id, so it rides
    # `_send` (which `_post`'s id-guard would reject) and posts the read-only Graph
    # v23.0 body (no typing_indicator) to /{phone_number_id}/messages.
    fake_httpx.typing_response = response(200, json={"success": True})
    await mark_read(PHONE_NUMBER_ID, "wamid.IN")

    assert len(fake_httpx.typing_calls) == 1
    signal = fake_httpx.typing_calls[0]
    assert signal["url"] == _MESSAGES_URL
    assert signal["json"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN",
    }
    assert "typing_indicator" not in signal["json"]
    assert signal["headers"]["Authorization"].startswith("Bearer ")


async def test_mark_read_raises_channel_delivery_error_on_rejection(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # `_send` classifies a non-2xx into ChannelDeliveryError; the inbound caller is
    # the one that swallows it, so the client function itself still raises loudly.
    fake_httpx.typing_response = response(500, text="messages endpoint down")
    with pytest.raises(ChannelDeliveryError):
        await mark_read(PHONE_NUMBER_ID, "wamid.IN")


async def test_mark_read_typing_rides_send_and_tolerates_no_message_id(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Meta's combined mark-as-read + typing-indicator send answers {"success": true}
    # with NO messages[].id. It must ride `_send` (which `_post`'s id-guard would
    # reject) and post the exact Graph v23.0 body to /{phone_number_id}/messages.
    fake_httpx.typing_response = response(200, json={"success": True})
    await mark_read_typing(PHONE_NUMBER_ID, "wamid.IN")

    assert len(fake_httpx.typing_calls) == 1
    signal = fake_httpx.typing_calls[0]
    assert signal["url"] == _MESSAGES_URL
    assert signal["json"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN",
        "typing_indicator": {"type": "text"},
    }
    assert signal["headers"]["Authorization"].startswith("Bearer ")


async def test_mark_read_typing_raises_channel_delivery_error_on_rejection(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # `_send` classifies a non-2xx into ChannelDeliveryError; the inbound caller is
    # the one that swallows it, so the client function itself still raises loudly.
    fake_httpx.typing_response = response(500, text="typing endpoint down")
    with pytest.raises(ChannelDeliveryError):
        await mark_read_typing(PHONE_NUMBER_ID, "wamid.IN")
