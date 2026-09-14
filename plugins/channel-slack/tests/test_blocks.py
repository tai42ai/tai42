"""Slack block and mrkdwn rendering helpers: option enumeration, control-character
escaping, reply-value decoding, and OpenStreetMap URL formatting.
"""

from __future__ import annotations

import pytest
from tai42_contract.channels import (
    OPTION_ID_MAX_CHARS,
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

from tai42_channel_slack.blocks import (
    _osm_url,
    build_flat_option_blocks,
    build_footer_block,
    build_location_block,
    build_media_blocks,
    build_section_blocks,
    decode_reply_value,
    encode_reply_value,
)
from tai42_channel_slack.channel import _render_text

from .conftest import (
    make_delivery,
)

pytestmark = pytest.mark.usefixtures("slack_env")


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


def test_link_caption_mrkdwn_control_chars_are_escaped_inside_the_link():
    # A caption carrying an mrkdwn control char must be escaped so the ``<url|caption>``
    # link stays intact — an unescaped ``>`` would close the link early and leak markup.
    media = [MediaItem(kind=MediaKind.LINK, url="https://docs.test/q", caption="Q3 > Q2")]
    blocks = build_media_blocks(media)
    assert blocks == [{"type": "section", "text": {"type": "mrkdwn", "text": "<https://docs.test/q|Q3 &gt; Q2>"}}]


def test_location_name_and_address_mrkdwn_control_chars_are_escaped():
    # An author-set place name with ``&`` and angle brackets is escaped in the mrkdwn
    # section, never rendered as markup.
    location = LocationElement(latitude=1.0, longitude=2.0, name="Joe & <b>Co</b>", address="A & B St")
    text = build_location_block(location)["text"]["text"]
    assert text.startswith("*Joe &amp; &lt;b&gt;Co&lt;/b&gt;*")
    assert "\nA &amp; B St\n" in text


def test_footer_mrkdwn_control_chars_are_escaped():
    # A footer carrying ``&`` and angle brackets renders as literal text in the context block.
    block = build_footer_block("Terms & <conditions>")
    assert block == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Terms &amp; &lt;conditions&gt;"}],
    }


def test_section_title_mrkdwn_control_chars_are_escaped():
    # An author-set section title with ``&`` and angle brackets is escaped in its mrkdwn
    # header, never rendered as markup.
    sections = [OptionSection(title="Docs & <links>", rows=[ReplyOption(text="Open")])]
    blocks = build_section_blocks(sections)
    assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "*Docs &amp; &lt;links&gt;*"}}


def test_over_cap_link_option_degrades_to_mrkdwn_section_with_escaped_url():
    # An over-cap option group degrades to an mrkdwn section (not plain_text), so a link
    # option's ``<url|label>`` renders as a link and its ``&`` is url-escaped rather than
    # printed literally.
    options: list[Option] = [LinkOption(label="A" * 80, url="https://app.test/x?a=1&b=2")]
    blocks = build_flat_option_blocks(options)
    assert blocks == [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"• <https://app.test/x?a=1&amp;b=2|{'A' * 80}>"}}
    ]


def test_decode_reply_value_clamps_an_oversized_forged_id():
    # A forged-signed oversized id must be dropped here (the text still bridges), so it can
    # never reach validate_entry_params to raise and 5xx-loop the webhook.
    oversized = "x" * (OPTION_ID_MAX_CHARS + 1)
    value = encode_reply_value("Yes", oversized)
    text, reply_id = decode_reply_value(value)
    assert (text, reply_id) == ("Yes", None)


def test_decode_reply_value_keeps_a_max_length_id():
    # An id exactly at the bound is legitimate and survives the round trip.
    at_max = "x" * OPTION_ID_MAX_CHARS
    text, reply_id = decode_reply_value(encode_reply_value("Yes", at_max))
    assert (text, reply_id) == ("Yes", at_max)


def test_osm_url_renders_near_zero_coordinate_as_plain_decimal():
    # A near-zero coordinate must render as a plain decimal, never scientific notation
    # (``1e-07``), which OpenStreetMap would not parse.
    url = _osm_url(0.0000001, -0.0000002)
    assert url == "https://www.openstreetmap.org/?mlat=0.0000001&mlon=-0.0000002#map=16/0.0000001/-0.0000002"


def test_osm_url_trims_trailing_zeros_on_short_coordinates():
    assert _osm_url(51.5, -0.12) == "https://www.openstreetmap.org/?mlat=51.5&mlon=-0.12#map=16/51.5/-0.12"
