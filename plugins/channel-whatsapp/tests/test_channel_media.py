"""Media-item sends — the notify media prelude, the deliver media-ahead-of-question
ordering, location messages, and each file-media type sent natively."""

from __future__ import annotations

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelNotification
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind

from tai42_channel_whatsapp.channel import WhatsAppChannel

from .conftest import _MESSAGES_URL, ALLOWED_A, FakeHttpx, FakeRedis, _accepted, make_delivery, response

pytestmark = pytest.mark.usefixtures("whatsapp_env")


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
                MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Details page"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/b.jpg"),
            ],
        )
    )

    assert ids == ["wamid.BODY", "wamid.IMG1", "wamid.IMG2"]  # every wamid, in send order
    # Body first, with the link item appended as a text line.
    assert fake_httpx.calls[0]["json"]["type"] == "text"
    assert fake_httpx.calls[0]["json"]["text"]["body"] == "Here are the photos.\nDetails page: https://docs.example/p/1"
    # Then each image as its own image message, in order.
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/a.jpg", "caption": "Front"}
    assert fake_httpx.calls[2]["json"]["image"] == {"link": "https://cdn.example/b.jpg"}


async def test_notify_media_only_images_skip_the_body_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A media-only notification (blank message, images only) sends NO text body — just each
    # image message, in order. WhatsApp has no empty-body send, so the body is skipped entirely.
    fake_httpx.responses.append(_accepted("wamid.IMG1"))
    fake_httpx.responses.append(_accepted("wamid.IMG2"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="",
            recipient=ALLOWED_A,
            media=[
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg", caption="Front"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/b.jpg"),
            ],
        )
    )

    assert ids == ["wamid.IMG1", "wamid.IMG2"]
    assert len(fake_httpx.calls) == 2  # no body message — only the two images
    assert fake_httpx.calls[0]["json"]["image"] == {"link": "https://cdn.example/a.jpg", "caption": "Front"}
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/b.jpg"}


async def test_notify_media_only_with_links_renders_links_as_the_body(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A media-only notification whose media carries a link item renders that link AS the body
    # text (no leading blank line from the empty message), then any images.
    fake_httpx.responses.append(_accepted("wamid.BODY"))
    fake_httpx.responses.append(_accepted("wamid.IMG1"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="",
            recipient=ALLOWED_A,
            media=[
                MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Details page"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/b.jpg"),
            ],
        )
    )

    assert ids == ["wamid.BODY", "wamid.IMG1"]
    assert fake_httpx.calls[0]["json"]["text"]["body"] == "Details page: https://docs.example/p/1"
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/b.jpg"}


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


async def test_deliver_media_sends_links_then_images_then_question(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A delivered question's display media rides AHEAD of the question: any link items
    # as one text line-block, then each image as its own image message, then the
    # question itself last (the actionable message at the foot of the chat).
    fake_httpx.responses.append(_accepted("wamid.LINKS"))
    fake_httpx.responses.append(_accepted("wamid.IMG1"))
    fake_httpx.responses.append(_accepted("wamid.IMG2"))
    fake_httpx.responses.append(_accepted("wamid.Q"))

    await WhatsAppChannel().deliver(
        make_delivery(
            answer_format="text",
            question="Which one is broken?",
            media=[
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg", caption="Front"),
                MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Details page"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/b.jpg"),
            ],
        )
    )

    urls_and_types = [(call["url"], call["json"]["type"]) for call in fake_httpx.calls]
    assert urls_and_types == [
        (_MESSAGES_URL, "text"),  # the link line-block
        (_MESSAGES_URL, "image"),  # image A
        (_MESSAGES_URL, "image"),  # image B
        (_MESSAGES_URL, "text"),  # the question, last
    ]
    assert fake_httpx.calls[0]["json"]["text"]["body"] == "Details page: https://docs.example/p/1"
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/a.jpg", "caption": "Front"}
    assert fake_httpx.calls[2]["json"]["image"] == {"link": "https://cdn.example/b.jpg"}
    assert fake_httpx.calls[3]["json"]["text"]["body"] == "Which one is broken?"


async def test_deliver_media_images_only_precede_the_question(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # No links: just each image, then the question — no empty link text message.
    fake_httpx.responses.append(_accepted("wamid.IMG"))
    fake_httpx.responses.append(_accepted("wamid.Q"))

    await WhatsAppChannel().deliver(
        make_delivery(
            answer_format="text",
            question="See attached?",
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg")],
        )
    )

    assert [call["json"]["type"] for call in fake_httpx.calls] == ["image", "text"]
    assert fake_httpx.calls[0]["json"]["image"] == {"link": "https://cdn.example/a.jpg"}
    assert fake_httpx.calls[1]["json"]["text"]["body"] == "See attached?"


async def test_deliver_media_reserves_after_the_media_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A Tier-2 select ask with media: the media is sent BEFORE the reservation and the
    # interactive question, so the reserve-before-question invariant still holds.
    fake_httpx.responses.append(_accepted("wamid.IMG"))
    fake_httpx.responses.append(_accepted("wamid.Q"))

    await WhatsAppChannel().deliver(
        make_delivery(
            answer_format="select",
            options=["a", "b"],
            question="Pick",
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg")],
        )
    )

    kinds = [kind for kind, _ in fake_redis.events]
    # image send, then reserve, then the interactive question send.
    assert kinds == ["http_post", "redis_set", "http_post"]
    assert fake_httpx.calls[1]["json"]["interactive"]["type"] == "button"


async def test_deliver_media_failure_raises_before_any_reservation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A media send that fails leaves nothing reserved — the media rides ahead of the
    # reservation, so the pair stays free and the failure is loud.
    fake_httpx.responses.append(response(400, json={"error": {"code": 131053, "message": "media download error"}}))

    with pytest.raises(ChannelDeliveryError, match="after delivering"):
        await WhatsAppChannel().deliver(
            make_delivery(
                answer_format="text",
                media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/bad.jpg")],
            )
        )

    assert not fake_redis.store  # nothing reserved
    assert len(fake_httpx.calls) == 1  # the failed image only; the question was never sent


async def test_deliver_without_media_sends_only_the_question(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A text-only ask still sends exactly one message, the question.
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery(answer_format="text", question="Deploy to prod?"))

    assert len(fake_httpx.calls) == 1
    assert fake_httpx.calls[0]["json"]["text"]["body"] == "Deploy to prod?"


async def test_notify_location_sends_location_message(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.BODY"))
    fake_httpx.responses.append(_accepted("wamid.LOC"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Here is the venue",
            recipient=ALLOWED_A,
            location=LocationElement(latitude=51.5, longitude=-0.12, name="Office", address="1 High St"),
        )
    )

    assert ids == ["wamid.BODY", "wamid.LOC"]
    assert fake_httpx.calls[0]["json"]["text"]["body"] == "Here is the venue"
    assert fake_httpx.calls[1]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "location",
        "location": {"latitude": 51.5, "longitude": -0.12, "name": "Office", "address": "1 High St"},
    }


async def test_notify_location_only_blank_message_sends_just_location(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.LOC"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(message="", recipient=ALLOWED_A, location=LocationElement(latitude=1.0, longitude=2.0))
    )

    assert ids == ["wamid.LOC"]
    assert len(fake_httpx.calls) == 1
    assert fake_httpx.calls[0]["json"]["location"] == {"latitude": 1.0, "longitude": 2.0}


async def test_notify_document_video_audio_media_each_send_native(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.BODY"))
    fake_httpx.responses.append(_accepted("wamid.DOC"))
    fake_httpx.responses.append(_accepted("wamid.VID"))
    fake_httpx.responses.append(_accepted("wamid.AUD"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Files attached.",
            recipient=ALLOWED_A,
            media=[
                MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.example/r.pdf", caption="Report", filename="r.pdf"),
                MediaItem(kind=MediaKind.VIDEO, url="https://cdn.example/clip.mp4", caption="Demo"),
                MediaItem(kind=MediaKind.AUDIO, url="https://cdn.example/a.mp3"),
            ],
        )
    )

    assert ids == ["wamid.BODY", "wamid.DOC", "wamid.VID", "wamid.AUD"]
    assert fake_httpx.calls[1]["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "document",
        "document": {"link": "https://cdn.example/r.pdf", "caption": "Report", "filename": "r.pdf"},
    }
    assert fake_httpx.calls[2]["json"]["video"] == {"link": "https://cdn.example/clip.mp4", "caption": "Demo"}
    assert fake_httpx.calls[3]["json"]["audio"] == {"link": "https://cdn.example/a.mp3"}
