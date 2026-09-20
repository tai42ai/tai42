"""Interactive notification rendering — reply buttons / list / cta_url shapes, the
wire-cap graceful degrade to numbered text, and minted-id collision-proofing."""

from __future__ import annotations

import pytest
from tai42_contract.channels import (
    ChannelInputError,
    ChannelNotification,
    LinkOption,
    Option,
    OptionSection,
    ReplyOption,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.channel import WhatsAppChannel

from .conftest import ALLOWED_A, FakeHttpx, FakeRedis, _accepted

pytestmark = pytest.mark.usefixtures("whatsapp_env")


async def test_notify_options_render_as_reply_buttons(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A notification's tappable options render as native reply buttons; each button's
    # id is the bare index (no interaction part), so a tap bridges its title as a
    # visitor message rather than being mistaken for a pending-ask answer.
    fake_httpx.responses.append(_accepted("wamid.OPT"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="How did we do?",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Great"), ReplyOption(text="Poor")],
        )
    )

    assert ids == ["wamid.OPT"]
    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "interactive"
    interactive = payload["interactive"]
    assert interactive["type"] == "button"
    assert interactive["body"] == {"text": "How did we do?"}
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "0", "title": "Great"}},
        {"type": "reply", "reply": {"id": "1", "title": "Poor"}},
    ]
    assert not fake_redis.store  # fire-and-forget: no correlation reserved


async def test_notify_reply_options_send_authored_ids_on_the_wire(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An AUTHORED reply-option id rides the wire verbatim (echoed back on tap as reply_id);
    # an option without one falls back to the minted 0-based index.
    fake_httpx.responses.append(_accepted("wamid.OPT"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="How did we do?",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Great", id="rating-great"), ReplyOption(text="Poor")],
        )
    )

    assert fake_httpx.calls[0]["json"]["interactive"]["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "rating-great", "title": "Great"}},
        {"type": "reply", "reply": {"id": "1", "title": "Poor"}},
    ]


async def test_notify_single_link_option_renders_as_cta_url(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A lone LinkOption → the single-URL cta_url interactive.
    fake_httpx.responses.append(_accepted("wamid.CTA"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Your report is ready.",
            recipient=ALLOWED_A,
            options=[LinkOption(label="View report", url="https://pay.example/42")],
        )
    )

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "cta_url"
    assert interactive["action"]["parameters"] == {"display_text": "View report", "url": "https://pay.example/42"}


async def test_notify_mixed_reply_and_link_options_buttons_with_link_body_line(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Reply + link options: replies render as reply buttons, the link is appended to the body
    # as a `label: url` line (a WhatsApp reply widget carries no URL button).
    fake_httpx.responses.append(_accepted("wamid.MIX"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Rate us",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Good"), LinkOption(label="Learn more", url="https://x.example/why")],
        )
    )

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "button"
    assert interactive["body"]["text"] == "Rate us\nLearn more: https://x.example/why"
    assert interactive["action"]["buttons"] == [{"type": "reply", "reply": {"id": "0", "title": "Good"}}]


async def test_notify_multiple_link_options_render_as_text_lines(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Two+ link options: no native multi-URL interactive — a plain text body of the link lines.
    fake_httpx.responses.append(_accepted("wamid.LINKS"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Choose",
            recipient=ALLOWED_A,
            options=[
                LinkOption(label="Docs", url="https://x.example/docs"),
                LinkOption(label="Blog", url="https://x.example/blog"),
            ],
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == "Choose\nDocs: https://x.example/docs\nBlog: https://x.example/blog"


async def test_notify_sections_render_as_multi_section_list_with_descriptions(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_httpx.responses.append(_accepted("wamid.SEC"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Pick a dish",
            recipient=ALLOWED_A,
            sections=[
                OptionSection(
                    title="Starters",
                    rows=[ReplyOption(text="Soup", description="Tomato basil", id="soup")],
                ),
                OptionSection(title="Mains", rows=[ReplyOption(text="Steak")]),
            ],
        )
    )

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"]["button"] == "Choose an option"
    assert interactive["action"]["sections"] == [
        {"title": "Starters", "rows": [{"id": "soup", "title": "Soup", "description": "Tomato basil"}]},
        {"title": "Mains", "rows": [{"id": "1", "title": "Steak"}]},  # minted global index for the un-id'd row
    ]


async def test_notify_reply_option_description_forces_list_over_buttons(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A described reply option cannot show its description on a button, so a small option set
    # that would otherwise be buttons renders as a list instead.
    fake_httpx.responses.append(_accepted("wamid.DESC"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Pick",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Yes", description="go ahead"), ReplyOption(text="No")],
        )
    )

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"]["sections"] == [
        {"rows": [{"id": "0", "title": "Yes", "description": "go ahead"}, {"id": "1", "title": "No"}]}
    ]


async def test_notify_interactive_header_and_footer_ride_the_message(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.HF"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="How did we do?",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Great"), ReplyOption(text="Poor")],
            header=MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/banner.jpg"),
            footer="Thanks for your feedback",
        )
    )

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["header"] == {"type": "image", "image": {"link": "https://cdn.example/banner.jpg"}}
    assert interactive["footer"] == {"text": "Thanks for your feedback"}


async def test_notify_audio_header_sent_ahead_of_interactive(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An audio header has no interactive-header slot on WhatsApp: it is sent as its own audio
    # message BEFORE the interactive, which then carries no header key.
    fake_httpx.responses.append(_accepted("wamid.AUDIO"))
    fake_httpx.responses.append(_accepted("wamid.INT"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Listen then choose",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="OK")],
            header=MediaItem(kind=MediaKind.AUDIO, url="https://cdn.example/a.mp3"),
        )
    )

    assert ids == ["wamid.AUDIO", "wamid.INT"]
    assert fake_httpx.calls[0]["json"]["type"] == "audio"
    assert "header" not in fake_httpx.calls[1]["json"]["interactive"]


async def test_notify_many_options_render_as_interactive_list(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.LIST"))
    options: list[Option] = [ReplyOption(text=f"choice-{i}") for i in range(5)]  # >3 → past the button, within list

    await WhatsAppChannel().notify(ChannelNotification(message="Pick one", recipient=ALLOWED_A, options=options))

    interactive = fake_httpx.calls[0]["json"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"]["button"] == "Choose an option"
    assert interactive["action"]["sections"][0]["rows"] == [{"id": str(i), "title": f"choice-{i}"} for i in range(5)]


async def test_notify_long_options_fall_to_numbered_text(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An option longer than the list row-title cap forces the numbered-text fallback for
    # the whole notification — the human types an option (which enters the conversation).
    fake_httpx.responses.append(_accepted("wamid.NUM"))
    options: list[Option] = [ReplyOption(text="short"), ReplyOption(text="x" * 25)]

    await WhatsAppChannel().notify(ChannelNotification(message="Pick", recipient=ALLOWED_A, options=options))

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == f"Pick\n1. short\n2. {'x' * 25}\nReply with the text of one option."


async def test_notify_options_and_media_send_choice_then_images(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Options MAY combine with media: the body (with any link lines) carries the
    # tappable choice, then each image rides as its own message.
    fake_httpx.responses.append(_accepted("wamid.OPT"))
    fake_httpx.responses.append(_accepted("wamid.IMG"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Rate it",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Good"), ReplyOption(text="Bad")],
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg")],
        )
    )

    assert ids == ["wamid.OPT", "wamid.IMG"]
    assert fake_httpx.calls[0]["json"]["interactive"]["type"] == "button"
    assert fake_httpx.calls[1]["json"]["image"] == {"link": "https://cdn.example/a.jpg"}


# --- Interactive wire-cap graceful degrade (contract-valid but over Meta's caps) ------------
# The contract admits strings far longer than WhatsApp's per-field wire caps, so a
# contract-valid notification can still exceed a cap. Each over-cap field degrades the WHOLE
# message one tier (never a truncated/over-cap value Meta would 400), mirroring the flat
# select ask's established _interactive_choice_kind discipline.


async def test_notify_over_cap_row_description_degrades_list_to_numbered_text(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A row description past the 72-char list-row cap cannot ride the list (and a described
    # option can never be a button), so the whole message degrades to numbered text — where
    # the description rides the numbered line whole (a degrade never drops content).
    fake_httpx.responses.append(_accepted("wamid.NUM"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Pick",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Yes", description="d" * 73), ReplyOption(text="No")],
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    expected = f"Pick\n1. Yes — {'d' * 73}\n2. No\nReply with the text of one option."
    assert payload["text"]["body"] == expected


async def test_notify_sectioned_degrade_carries_row_descriptions(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A sectioned list degraded by an over-cap row description carries that description
    # whole on the numbered line — a degrade never silently drops authored content.
    fake_httpx.responses.append(_accepted("wamid.NUM"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Pick a dish",
            recipient=ALLOWED_A,
            sections=[OptionSection(title="Soups", rows=[ReplyOption(text="Soup", description="d" * 73)])],
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == (f"Pick a dish\nSoups\n1. Soup — {'d' * 73}\nReply with the text of one option.")


async def test_notify_over_cap_section_title_degrades_list_to_numbered_text(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A section title past the 24-char cap forces the whole sectioned list to numbered text,
    # the authored titles riding as plain text lines (no cap on a plain-text send).
    fake_httpx.responses.append(_accepted("wamid.NUM"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Pick a dish",
            recipient=ALLOWED_A,
            sections=[OptionSection(title="s" * 25, rows=[ReplyOption(text="Soup")])],
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == f"Pick a dish\n{'s' * 25}\n1. Soup\nReply with the text of one option."


async def test_notify_over_cap_section_row_title_degrades_list_to_numbered_text(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A sectioned-list row title past the 24-char cap forces numbered text — the sections path
    # never checked row titles (or the body) before this fix.
    fake_httpx.responses.append(_accepted("wamid.NUM"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Pick",
            recipient=ALLOWED_A,
            sections=[OptionSection(title="Mains", rows=[ReplyOption(text="r" * 25)])],
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == f"Pick\nMains\n1. {'r' * 25}\nReply with the text of one option."


async def test_notify_over_cap_body_degrades_sections_to_numbered_text(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An interactive body past the 1024-char cap forces the sectioned list to numbered text —
    # the sections path never checked the body before this fix.
    fake_httpx.responses.append(_accepted("wamid.NUM"))
    long_message = "m" * 1025

    await WhatsAppChannel().notify(
        ChannelNotification(
            message=long_message,
            recipient=ALLOWED_A,
            sections=[OptionSection(title="Mains", rows=[ReplyOption(text="Steak")])],
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == f"{long_message}\nMains\n1. Steak\nReply with the text of one option."


async def test_notify_over_cap_footer_folds_into_body_and_interactive_still_renders(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A footer past the 60-char footer cap folds into the body as a trailing line and the
    # interactive footer is dropped; the small option set still renders as buttons (body+footer
    # fit the 1024 body cap).
    fake_httpx.responses.append(_accepted("wamid.BTN"))
    long_footer = "f" * 61

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="How did we do?",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Great"), ReplyOption(text="Poor")],
            footer=long_footer,
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "interactive"
    interactive = payload["interactive"]
    assert interactive["type"] == "button"
    assert interactive["body"]["text"] == f"How did we do?\n{long_footer}"
    assert "footer" not in interactive


async def test_notify_over_cap_cta_url_label_degrades_to_body_line(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A lone link whose display_text (label) exceeds the 20-char cta_url cap degrades to the
    # `label: url` body-line rendering instead of shipping an over-cap cta_url button.
    fake_httpx.responses.append(_accepted("wamid.LINK"))
    long_label = "L" * 21

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Your report is ready.",
            recipient=ALLOWED_A,
            options=[LinkOption(label=long_label, url="https://pay.example/42")],
        )
    )

    payload = fake_httpx.calls[0]["json"]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == f"Your report is ready.\n{long_label}: https://pay.example/42"


# --- Minted-id collision-proofing against authored ids --------------------------


async def test_notify_minted_id_steps_past_a_colliding_authored_id(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An authored numeric id beside an un-id'd sibling: the sibling's minted 0-based index would
    # equal the authored id, a wire collision Meta 400s (button/row ids must be unique). The
    # minted id steps to a deterministic non-colliding token instead.
    fake_httpx.responses.append(_accepted("wamid.BTN"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="How did we do?",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="Great", id="1"), ReplyOption(text="Poor")],
        )
    )

    buttons = fake_httpx.calls[0]["json"]["interactive"]["action"]["buttons"]
    wire_ids = [button["reply"]["id"] for button in buttons]
    assert wire_ids == ["1", "1#1"]
    assert len(set(wire_ids)) == len(wire_ids)  # unique across the message


async def test_notify_sectioned_minted_id_steps_past_a_colliding_authored_id(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Same collision-proofing across a sectioned list: the un-id'd row's minted GLOBAL index
    # would equal the authored id on its sibling; it steps to a non-colliding token.
    fake_httpx.responses.append(_accepted("wamid.SEC"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Pick",
            recipient=ALLOWED_A,
            sections=[OptionSection(title="S", rows=[ReplyOption(text="A", id="1"), ReplyOption(text="B")])],
        )
    )

    rows = fake_httpx.calls[0]["json"]["interactive"]["action"]["sections"][0]["rows"]
    wire_ids = [row["id"] for row in rows]
    assert wire_ids == ["1", "1#1"]
    assert len(set(wire_ids)) == len(wire_ids)


async def test_notify_duplicate_authored_ids_refused_pre_wire(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Two options carrying the SAME authored id is an author error the wire cannot express
    # (unique-id rule) — refused loudly BEFORE any send, never round-tripped to Meta.
    with pytest.raises(ChannelInputError, match="dup"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Pick",
                recipient=ALLOWED_A,
                options=[ReplyOption(text="A", id="dup"), ReplyOption(text="B", id="dup")],
            )
        )

    assert not fake_httpx.calls  # nothing sent
