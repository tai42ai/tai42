"""Contract tests for the extended channel-agnostic vocabulary: new media kinds + filename, the
shared location element, discriminated options + sectioned lists, interactive header/footer, the
restructured template components, the mirrored AnswerPart/ChannelNotification surface, and the
inbound/answer enrichment params seams.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from tai42_contract.channels import (
    NOTIFICATION_FOOTER_MAX_CHARS,
    NOTIFICATION_OPTION_MAX_CHARS,
    NOTIFICATION_OPTIONS_MAX,
    NOTIFICATION_SECTIONS_MAX,
    OPTION_ID_MAX_CHARS,
    TEMPLATE_BUTTONS_MAX,
    TEMPLATE_PARAM_MAX_CHARS,
    ChannelNotification,
    ChannelTemplate,
    InboundBridge,
    LinkOption,
    OptionSection,
    QuickReplyButtonParam,
    ReplyOption,
    TemplateButtonParam,
    UrlButtonParam,
)
from tai42_contract.conversations import AnswerPart, ConversationMessage
from tai42_contract.interactions.models import (
    LOCATION_ADDRESS_MAX_CHARS,
    LOCATION_NAME_MAX_CHARS,
    MEDIA_FILENAME_MAX_CHARS,
    InteractionResponse,
    LocationElement,
    MediaItem,
    MediaKind,
    validate_action_url,
)


def _image() -> MediaItem:
    return MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")


# -- MediaKind + MediaItem file kinds + filename ---------------------------------------


@pytest.mark.parametrize("kind", [MediaKind.DOCUMENT, MediaKind.VIDEO, MediaKind.AUDIO])
def test_new_file_kinds_accept_https_and_served_media(kind: MediaKind):
    assert MediaItem(kind=kind, url="https://cdn.example/f").kind is kind
    # A same-origin served-media reference is valid for every file kind.
    ref = "/api/interactions/media/" + "A" * 43
    assert MediaItem(kind=kind, url=ref).url == ref


@pytest.mark.parametrize("kind", [MediaKind.DOCUMENT, MediaKind.VIDEO, MediaKind.AUDIO])
def test_new_file_kinds_reject_data_uri(kind: MediaKind):
    # A data:image/* URI is IMAGE-only — every other file kind refuses it.
    with pytest.raises(ValidationError, match="only image media may carry a data:image"):
        MediaItem(kind=kind, url="data:image/png;base64,AAAA")


@pytest.mark.parametrize("kind", [MediaKind.DOCUMENT, MediaKind.VIDEO, MediaKind.AUDIO])
def test_new_file_kinds_reject_non_absolute(kind: MediaKind):
    with pytest.raises(ValidationError, match="absolute https URL or a served-media reference"):
        MediaItem(kind=kind, url="ftp://x/y")


def test_document_carries_filename():
    item = MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.example/d.pdf", filename="report.pdf")
    assert item.filename == "report.pdf"


@pytest.mark.parametrize("kind", [MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.LINK])
def test_filename_only_on_document(kind: MediaKind):
    with pytest.raises(ValidationError, match="filename is meaningful only for document"):
        MediaItem(kind=kind, url="https://cdn.example/x", filename="no.bin")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_filename_non_blank(blank: str):
    with pytest.raises(ValidationError, match="filename must be non-blank"):
        MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.example/d", filename=blank)


def test_filename_length_capped():
    with pytest.raises(ValidationError, match=f"at most {MEDIA_FILENAME_MAX_CHARS}"):
        MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.example/d", filename="x" * (MEDIA_FILENAME_MAX_CHARS + 1))


def test_filename_rejects_control_chars():
    with pytest.raises(ValidationError, match="single-line label"):
        MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.example/d", filename="a\nb.pdf")


def test_image_still_accepts_data_uri_and_https():
    assert MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAA").kind is MediaKind.IMAGE
    assert MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png").kind is MediaKind.IMAGE


# -- LocationElement -------------------------------------------------------------------


def test_location_minimal_and_full():
    assert LocationElement(latitude=0.0, longitude=0.0).name is None
    full = LocationElement(latitude=51.5, longitude=-0.12, name="HQ", address="1 High St")
    assert (full.name, full.address) == ("HQ", "1 High St")


@pytest.mark.parametrize("lat", [-90.1, 90.1, 200.0])
def test_location_latitude_bounds(lat: float):
    with pytest.raises(ValidationError, match="latitude must be within"):
        LocationElement(latitude=lat, longitude=0.0)


@pytest.mark.parametrize("lon", [-180.1, 180.1])
def test_location_longitude_bounds(lon: float):
    with pytest.raises(ValidationError, match="longitude must be within"):
        LocationElement(latitude=0.0, longitude=lon)


def test_location_name_and_address_bounds():
    with pytest.raises(ValidationError, match="location name must be at most"):
        LocationElement(latitude=0, longitude=0, name="x" * (LOCATION_NAME_MAX_CHARS + 1))
    with pytest.raises(ValidationError, match="location address must be at most"):
        LocationElement(latitude=0, longitude=0, address="x" * (LOCATION_ADDRESS_MAX_CHARS + 1))
    with pytest.raises(ValidationError, match="location name must be non-blank"):
        LocationElement(latitude=0, longitude=0, name="   ")
    with pytest.raises(ValidationError, match="single-line label"):
        LocationElement(latitude=0, longitude=0, address="line1\nline2")


def test_location_is_frozen():
    loc = LocationElement(latitude=1, longitude=2)
    with pytest.raises(ValidationError):
        loc.latitude = 3  # type: ignore[misc]


# -- validate_action_url + options -----------------------------------------------------


def test_validate_action_url_accepts_http_and_https():
    assert validate_action_url("https://x.example/p") == "https://x.example/p"
    assert validate_action_url("http://x.example/p") == "http://x.example/p"


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("   ", "non-blank"),
        ("not-a-url", "absolute http"),
        ("https://x.example/\np", "single-line"),
    ],
)
def test_validate_action_url_rejects(url: str, match: str):
    with pytest.raises(ValueError, match=match):
        validate_action_url(url)


def test_reply_and_link_options():
    r = ReplyOption(text="Yes", description="pick yes")
    assert (r.kind, r.text, r.description) == ("reply", "Yes", "pick yes")
    lk = LinkOption(label="Docs", url="https://x.example/d")
    assert (lk.kind, lk.label, lk.url) == ("link", "Docs", "https://x.example/d")


def test_reply_option_bounds():
    with pytest.raises(ValidationError, match="reply option text must be non-blank"):
        ReplyOption(text="  ")
    with pytest.raises(ValidationError, match="reply option text must be at most"):
        ReplyOption(text="x" * (NOTIFICATION_OPTION_MAX_CHARS + 1))
    with pytest.raises(ValidationError, match="reply option description must be non-blank"):
        ReplyOption(text="ok", description="  ")


def test_reply_option_authored_id_roundtrips_flat_and_in_a_section():
    # An author-set id rides a flat option AND a sectioned-list row (rows are ReplyOptions).
    flat = ReplyOption(text="Yes", id="opt-yes")
    assert flat.id == "opt-yes"
    section = OptionSection(title="Fruit", rows=[ReplyOption(text="Apple", id="row-apple")])
    assert section.rows[0].id == "row-apple"
    # Absent by default — the channel mints its own id as today.
    assert ReplyOption(text="Yes").id is None


def test_reply_option_id_bounds():
    with pytest.raises(ValidationError, match="reply option id must be non-blank"):
        ReplyOption(text="Yes", id="  ")
    with pytest.raises(ValidationError, match=f"at most {OPTION_ID_MAX_CHARS}"):
        ReplyOption(text="Yes", id="x" * (OPTION_ID_MAX_CHARS + 1))
    with pytest.raises(ValidationError, match="single-line token"):
        ReplyOption(text="Yes", id="a b")
    with pytest.raises(ValidationError, match="single-line token"):
        ReplyOption(text="Yes", id="a\nb")


def test_link_option_bounds():
    with pytest.raises(ValidationError, match="link option label must be non-blank"):
        LinkOption(label=" ", url="https://x.example")
    with pytest.raises(ValidationError, match="absolute http"):
        LinkOption(label="Go", url="notaurl")


def test_option_is_discriminated_bare_string_rejected():
    with pytest.raises(ValidationError):
        ChannelNotification(message="m", options=["bare"])  # type: ignore[list-item]


# -- OptionSection + sectioned lists ---------------------------------------------------


def test_option_section_roundtrips():
    sec = OptionSection(title="Fruit", rows=[ReplyOption(text="Apple"), ReplyOption(text="Pear", description="green")])
    n = ChannelNotification(message="menu", sections=[sec])
    assert n.sections == [sec]


def test_section_rows_non_empty_and_title_blank():
    with pytest.raises(ValidationError, match="section rows must be a non-empty list"):
        OptionSection(title="t", rows=[])
    with pytest.raises(ValidationError, match="section title must be non-blank"):
        OptionSection(title="  ", rows=[ReplyOption(text="a")])


def test_sections_count_capped():
    secs = [OptionSection(title=f"s{i}", rows=[ReplyOption(text="r")]) for i in range(NOTIFICATION_SECTIONS_MAX)]
    assert ChannelNotification(message="m", sections=secs).sections == secs
    with pytest.raises(ValidationError, match=f"at most {NOTIFICATION_SECTIONS_MAX} sections"):
        ChannelNotification(message="m", sections=[*secs, OptionSection(title="x", rows=[ReplyOption(text="r")])])


def test_sections_total_rows_capped():
    # One section with more than NOTIFICATION_OPTIONS_MAX rows trips the summed-rows cap.
    rows = [ReplyOption(text=f"r{i}") for i in range(NOTIFICATION_OPTIONS_MAX + 1)]
    with pytest.raises(ValidationError, match=f"at most {NOTIFICATION_OPTIONS_MAX} rows in total"):
        ChannelNotification(message="m", sections=[OptionSection(title="s", rows=rows)])


def test_options_xor_sections():
    with pytest.raises(ValidationError, match="options and sections are mutually exclusive"):
        ChannelNotification(
            message="m",
            options=[ReplyOption(text="a")],
            sections=[OptionSection(title="s", rows=[ReplyOption(text="r")])],
        )


# -- header / footer -------------------------------------------------------------------


def test_header_and_footer_on_interactive():
    n = ChannelNotification(message="m", options=[ReplyOption(text="a")], header=_image(), footer="ft")
    assert n.header == _image()
    assert n.footer == "ft"


def test_header_requires_choice_surface():
    with pytest.raises(ValidationError, match="header requires options or sections"):
        ChannelNotification(message="m", header=_image())


def test_footer_requires_choice_surface():
    with pytest.raises(ValidationError, match="footer requires options or sections"):
        ChannelNotification(message="m", footer="ft")


def test_footer_bounds():
    with pytest.raises(ValidationError, match="footer must be non-blank"):
        ChannelNotification(message="m", options=[ReplyOption(text="a")], footer="  ")
    with pytest.raises(ValidationError, match=f"at most {NOTIFICATION_FOOTER_MAX_CHARS}"):
        ChannelNotification(
            message="m", options=[ReplyOption(text="a")], footer="x" * (NOTIFICATION_FOOTER_MAX_CHARS + 1)
        )


def test_header_rejects_link_kind():
    link = MediaItem(kind=MediaKind.LINK, url="https://x.example/p")
    with pytest.raises(ValidationError, match="header media must be a display item"):
        ChannelNotification(message="m", options=[ReplyOption(text="a")], header=link)


# -- location on carriers --------------------------------------------------------------


def test_location_only_notification():
    loc = LocationElement(latitude=1, longitude=2)
    n = ChannelNotification(message="", location=loc)
    assert n.location == loc


def test_location_combines_with_options_and_media():
    loc = LocationElement(latitude=1, longitude=2)
    n = ChannelNotification(message="here", location=loc, media=[_image()], options=[ReplyOption(text="ok")])
    assert n.location == loc


def test_location_excludes_template():
    with pytest.raises(ValidationError, match="location and template are mutually exclusive"):
        ChannelNotification(
            message="m",
            location=LocationElement(latitude=1, longitude=2),
            template=ChannelTemplate(name="t", language="en"),
        )


def test_blank_message_needs_content():
    with pytest.raises(ValidationError, match="non-blank unless media or location"):
        ChannelNotification(message="")


# -- ChannelTemplate components --------------------------------------------------------


def test_template_component_parameters():
    t = ChannelTemplate(
        name="order_update",
        language="en",
        header_media=_image(),
        body_parameters=["A-1", "shipped"],
        buttons=[QuickReplyButtonParam(payload="track"), UrlButtonParam(url_parameter="A-1")],
    )
    assert t.header_media == _image()
    assert t.body_parameters == ["A-1", "shipped"]
    assert [b.kind for b in t.buttons] == ["quick_reply", "url"]


def test_template_header_media_rejects_link():
    link = MediaItem(kind=MediaKind.LINK, url="https://x.example/p")
    with pytest.raises(ValidationError, match="header_media must be a display item"):
        ChannelTemplate(name="t", language="en", header_media=link)


def test_template_body_parameters_bounds():
    with pytest.raises(ValidationError, match="each body parameter must be non-blank"):
        ChannelTemplate(name="t", language="en", body_parameters=["ok", "  "])
    with pytest.raises(ValidationError, match=f"at most {TEMPLATE_PARAM_MAX_CHARS}"):
        ChannelTemplate(name="t", language="en", body_parameters=["x" * (TEMPLATE_PARAM_MAX_CHARS + 1)])


def test_template_buttons_capped():
    buttons: list[TemplateButtonParam] = [
        QuickReplyButtonParam(payload=f"p{i}") for i in range(TEMPLATE_BUTTONS_MAX + 1)
    ]
    with pytest.raises(ValidationError, match=f"at most {TEMPLATE_BUTTONS_MAX}"):
        ChannelTemplate(name="t", language="en", buttons=buttons)


def test_template_button_param_bounds():
    with pytest.raises(ValidationError, match="quick-reply button payload must be non-blank"):
        QuickReplyButtonParam(payload="  ")
    with pytest.raises(ValidationError, match="url button parameter must be non-blank"):
        UrlButtonParam(url_parameter="  ")


def test_template_is_frozen():
    t = ChannelTemplate(name="t", language="en")
    with pytest.raises(ValidationError):
        t.name = "x"  # type: ignore[misc]


# -- AnswerPart mirror -----------------------------------------------------------------


def test_answer_part_mirrors_full_surface():
    part = AnswerPart(
        message="menu",
        sections=[OptionSection(title="s", rows=[ReplyOption(text="r", description="d")])],
        header=_image(),
        footer="ft",
    )
    assert not part.is_plain_text()
    assert part.footer == "ft"


def test_answer_part_location_only_not_plain_text():
    part = AnswerPart(message="", location=LocationElement(latitude=1, longitude=2))
    assert not part.is_plain_text()


def test_answer_part_options_xor_sections():
    with pytest.raises(ValidationError, match="options and sections are mutually exclusive"):
        AnswerPart(
            message="m",
            options=[ReplyOption(text="a")],
            sections=[OptionSection(title="s", rows=[ReplyOption(text="r")])],
        )


def test_answer_part_header_requires_choice():
    with pytest.raises(ValidationError, match="header requires options or sections"):
        AnswerPart(message="m", header=_image())


def test_answer_part_link_option():
    part = AnswerPart(message="m", options=[LinkOption(label="Go", url="https://x.example")])
    assert part.options is not None
    assert isinstance(part.options[0], LinkOption)


# -- inbound accept params: attachments / location on ConversationMessage --------------


def test_conversation_message_attachments_and_location():
    msg = ConversationMessage(
        external_user_id="u",
        text="see attached",
        attachments=[MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.example/d.pdf", filename="d.pdf")],
        location=LocationElement(latitude=1, longitude=2),
    )
    assert msg.attachments is not None
    assert msg.attachments[0].filename == "d.pdf"
    assert msg.location is not None


def test_conversation_message_attachments_list_caps():
    with pytest.raises(ValidationError, match="non-empty list when present"):
        ConversationMessage(external_user_id="u", text="hi", attachments=[])


def test_conversation_message_defaults_are_none():
    msg = ConversationMessage(external_user_id="u", text="hi")
    assert msg.attachments is None
    assert msg.location is None


# -- answer-path enrichment params: InboundBridge + InteractionResponse -----------------


def test_inbound_bridge_params_roundtrip_and_validation():
    bridge = InboundBridge(
        channel_id="c",
        our_identity="o",
        client_address="a",
        cap_key="k",
        provider_message_id="p",
        bridge_text="hi",
        params={"reply_id": "wamid.X", "referral": "ad-42"},
    )
    assert bridge.params == {"reply_id": "wamid.X", "referral": "ad-42"}
    # Absent params = today's behavior.
    assert (
        InboundBridge(
            channel_id="c", our_identity="o", client_address="a", cap_key="k", provider_message_id="p", bridge_text="hi"
        ).params
        is None
    )
    with pytest.raises(ValidationError, match="must match"):
        InboundBridge(
            channel_id="c",
            our_identity="o",
            client_address="a",
            cap_key="k",
            provider_message_id="p",
            bridge_text="hi",
            params={"bad key": "x"},
        )


def test_on_mismatch_policy_defaults_and_rides_the_shapes():
    from datetime import timedelta

    from tai42_contract.channels import Correlation, InboundAnswerOutcome
    from tai42_contract.interactions.models import AnswerMismatchPolicy, InteractionRequest

    now = datetime.now(UTC)
    # Default is RETRY on every shape (zero behavior change for an existing ask).
    ask = InteractionRequest(
        interaction_id="i",
        group_id="g",
        question="pick",
        reply_to="r",
        created_at=now,
        timeout_at=now + timedelta(minutes=5),
    )
    assert ask.on_mismatch is AnswerMismatchPolicy.RETRY
    # An ask may author the bridge (digression) policy.
    bridged_ask = InteractionRequest(
        interaction_id="i",
        group_id="g",
        question="pick",
        reply_to="r",
        created_at=now,
        timeout_at=now + timedelta(minutes=5),
        on_mismatch=AnswerMismatchPolicy.BRIDGE,
    )
    assert bridged_ask.on_mismatch is AnswerMismatchPolicy.BRIDGE
    # It rides the correlation the ladder reads, defaulting to RETRY.
    entry = Correlation(callback_url="https://x/cb", interaction_id="i", ttl_deadline=now + timedelta(minutes=5))
    assert entry.on_mismatch is AnswerMismatchPolicy.RETRY
    entry_bridge = Correlation(
        callback_url="https://x/cb",
        interaction_id="i",
        ttl_deadline=now + timedelta(minutes=5),
        on_mismatch=AnswerMismatchPolicy.BRIDGE,
    )
    assert entry_bridge.on_mismatch is AnswerMismatchPolicy.BRIDGE
    # The digression outcome exists for the ladder to return.
    assert InboundAnswerOutcome.BRIDGED_KEPT.value == "bridged_kept"


def test_channel_delivery_carries_on_mismatch():
    from tai42_contract.channels import ChannelDelivery
    from tai42_contract.interactions.models import AnswerMismatchPolicy

    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "interaction_id": "i",
        "question": "pick",
        "answer_format": "text",
        "callback_url": "https://x/cb",
        "timeout_at": now,
    }
    assert ChannelDelivery(**base).on_mismatch is AnswerMismatchPolicy.RETRY
    assert ChannelDelivery(**base, on_mismatch=AnswerMismatchPolicy.BRIDGE).on_mismatch is AnswerMismatchPolicy.BRIDGE


def test_mismatch_notice_defaults_bounds_and_rides_the_shapes():
    from datetime import timedelta

    from tai42_contract.channels import ChannelDelivery, Correlation
    from tai42_contract.interactions.models import MISMATCH_NOTICE_MAX_CHARS, InteractionRequest

    now = datetime.now(UTC)

    def _ask(**over: Any):
        return InteractionRequest(
            interaction_id="i",
            group_id="g",
            question="pick",
            reply_to="r",
            created_at=now,
            timeout_at=now + timedelta(minutes=5),
            **over,
        )

    # Default None (built-in notice); a set notice rides verbatim.
    assert _ask().mismatch_notice is None
    assert _ask(mismatch_notice="Please pick a listed option ({reason}).").mismatch_notice == (
        "Please pick a listed option ({reason})."
    )
    with pytest.raises(ValidationError, match="mismatch_notice must be non-blank"):
        _ask(mismatch_notice="   ")
    with pytest.raises(ValidationError, match=f"at most {MISMATCH_NOTICE_MAX_CHARS}"):
        _ask(mismatch_notice="x" * (MISMATCH_NOTICE_MAX_CHARS + 1))

    # It rides the delivery and the parked correlation the ladder reads.
    delivery = ChannelDelivery(
        interaction_id="i",
        question="pick",
        answer_format="text",
        callback_url="https://x/cb",
        timeout_at=now,
        mismatch_notice="Try again: {reason}",
    )
    assert delivery.mismatch_notice == "Try again: {reason}"
    entry = Correlation(
        callback_url="https://x/cb",
        interaction_id="i",
        ttl_deadline=now + timedelta(minutes=5),
        mismatch_notice="Try again: {reason}",
    )
    assert entry.mismatch_notice == "Try again: {reason}"

    # ChannelDelivery re-validates the notice defensively — the SAME non-blank + cap bound the
    # ask REQUEST enforces, symmetric with its media re-validation, so the delivery frame is
    # bounded exactly as the ask that produced it.
    assert (
        ChannelDelivery(
            interaction_id="i", question="pick", answer_format="text", callback_url="https://x/cb", timeout_at=now
        ).mismatch_notice
        is None
    )
    with pytest.raises(ValidationError, match="mismatch_notice must be non-blank"):
        ChannelDelivery(
            interaction_id="i",
            question="pick",
            answer_format="text",
            callback_url="https://x/cb",
            timeout_at=now,
            mismatch_notice="   ",
        )
    with pytest.raises(ValidationError, match=f"at most {MISMATCH_NOTICE_MAX_CHARS}"):
        ChannelDelivery(
            interaction_id="i",
            question="pick",
            answer_format="text",
            callback_url="https://x/cb",
            timeout_at=now,
            mismatch_notice="x" * (MISMATCH_NOTICE_MAX_CHARS + 1),
        )


def test_interaction_response_params_roundtrip_and_validation():
    resp = InteractionResponse(
        interaction_id="i",
        answer="yes",
        answered_by="ext",
        answered_at=datetime.now(UTC),
        params={"reply_id": "wamid.Y"},
    )
    assert resp.params == {"reply_id": "wamid.Y"}
    # Absent = today's plain envelope.
    assert (
        InteractionResponse(interaction_id="i", answer="yes", answered_by="ext", answered_at=datetime.now(UTC)).params
        is None
    )
    with pytest.raises(ValidationError, match="over the"):
        InteractionResponse(
            interaction_id="i",
            answer="yes",
            answered_by="ext",
            answered_at=datetime.now(UTC),
            params={"k": "x" * 600},
        )
