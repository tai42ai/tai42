"""Tests for ``ChannelNotification`` — one fire-and-forget message: its frozen model,
message/address caps, media caps, and the ask-less form schema/prefill fields."""

from __future__ import annotations

from typing import Any

import pytest


def _image_item() -> Any:
    from tai42_contract.interactions.models import MediaItem, MediaKind

    return MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/product.jpg", caption="A product")


def _form_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"size": {"type": "string"}}}


def test_notification_model_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    notification = ChannelNotification(message="deploy finished")
    with pytest.raises(ValidationError):
        notification.message = "changed"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_notification_rejects_blank_message(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"non-blank"):
        ChannelNotification(message=blank)


def test_notification_message_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_MESSAGE_MAX_CHARS, ChannelNotification

    at_cap = "x" * NOTIFICATION_MESSAGE_MAX_CHARS
    assert ChannelNotification(message=at_cap).message == at_cap
    with pytest.raises(ValidationError, match=r"at most"):
        ChannelNotification(message="x" * (NOTIFICATION_MESSAGE_MAX_CHARS + 1))


def test_notification_recipient_defaults_to_none_and_accepts_an_address():
    from tai42_contract.channels import ChannelNotification

    assert ChannelNotification(message="hi").recipient is None
    assert ChannelNotification(message="hi", recipient="@ops-team").recipient == "@ops-team"


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_notification_recipient_rejects_empty_when_present(empty: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"non-empty address"):
        ChannelNotification(message="hi", recipient=empty)


def test_notification_sender_identity_defaults_to_none_and_accepts_an_address():
    from tai42_contract.channels import ChannelNotification

    assert ChannelNotification(message="hi").sender_identity is None
    assert ChannelNotification(message="hi", sender_identity="+15550001111").sender_identity == "+15550001111"


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_notification_sender_identity_rejects_empty_when_present(empty: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"non-empty address"):
        ChannelNotification(message="hi", sender_identity=empty)


def test_notification_recipient_capped():
    # recipient is a short routing value that persists into the replayed record, so it
    # is length-capped just as the message is.
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_ADDRESS_MAX_CHARS, ChannelNotification

    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS
    assert ChannelNotification(message="hi", recipient=at_cap).recipient == at_cap
    with pytest.raises(ValidationError, match=r"address must be at most"):
        ChannelNotification(message="hi", recipient="x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1))


def test_notification_sender_identity_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_ADDRESS_MAX_CHARS, ChannelNotification

    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS
    assert ChannelNotification(message="hi", sender_identity=at_cap).sender_identity == at_cap
    with pytest.raises(ValidationError, match=r"address must be at most"):
        ChannelNotification(message="hi", sender_identity="x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1))


def test_notification_accepts_media():
    from tai42_contract.channels import ChannelNotification

    item = _image_item()
    notification = ChannelNotification(message="here it is", media=[item])
    assert notification.media == [item]
    # Absent by default; freeform text needs neither field.
    assert ChannelNotification(message="hi").media is None


def test_notification_media_rejects_present_but_empty_list():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"non-empty list"):
        ChannelNotification(message="hi", media=[])


def test_notification_media_over_max_items_raises():
    # The item-count cap mirrors InteractionRequest — one notification cannot fan out an
    # unbounded media list into the durable record and the frame it replays in.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_MAX_ITEMS, MediaItem, MediaKind

    items = [MediaItem(kind=MediaKind.IMAGE, url=f"https://host/{i}.png") for i in range(MEDIA_MAX_ITEMS + 1)]
    with pytest.raises(ValidationError, match=f"at most {MEDIA_MAX_ITEMS} items"):
        ChannelNotification(message="hi", media=items)


def test_notification_media_at_max_items_accepted():
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_MAX_ITEMS, MediaItem, MediaKind

    items = [MediaItem(kind=MediaKind.IMAGE, url=f"https://host/{i}.png") for i in range(MEDIA_MAX_ITEMS)]
    notification = ChannelNotification(message="hi", media=items)
    assert notification.media is not None
    assert len(notification.media) == MEDIA_MAX_ITEMS


def test_notification_media_total_uri_budget_raises():
    # Each item is within the per-item data: cap, but the summed url text exceeds the
    # per-notification MEDIA_TOTAL_URI_CHARS budget.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_DATA_URI_MAX_CHARS, MEDIA_TOTAL_URI_CHARS, MediaItem, MediaKind

    per_item = "data:image/png;base64," + "A" * 400_000
    assert len(per_item) <= MEDIA_DATA_URI_MAX_CHARS
    items = [MediaItem(kind=MediaKind.IMAGE, url=per_item) for _ in range(3)]
    assert sum(len(item.url) for item in items) > MEDIA_TOTAL_URI_CHARS
    with pytest.raises(ValidationError, match=f"total url length must be at most {MEDIA_TOTAL_URI_CHARS}"):
        ChannelNotification(message="hi", media=items)


def test_notification_media_total_uri_budget_at_cap_accepted():
    # The total budget is a strict ``>``; a list summing to EXACTLY MEDIA_TOTAL_URI_CHARS
    # is accepted (guards a ``>`` -> ``>=`` regression).
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_TOTAL_URI_CHARS, MediaItem, MediaKind

    prefix = "data:image/png;base64,"
    half = MEDIA_TOTAL_URI_CHARS // 2
    url_a = prefix + "A" * (half - len(prefix))
    url_b = prefix + "B" * (MEDIA_TOTAL_URI_CHARS - half - len(prefix))
    items = [MediaItem(kind=MediaKind.IMAGE, url=url_a), MediaItem(kind=MediaKind.IMAGE, url=url_b)]
    assert sum(len(item.url) for item in items) == MEDIA_TOTAL_URI_CHARS
    notification = ChannelNotification(message="hi", media=items)
    assert notification.media is not None
    assert len(notification.media) == 2


def test_notification_accepts_a_form_schema():
    # An ask-less form: the message is the form's prompt, the schema the fillable form;
    # the submission enters the conversation as a participant message.
    from tai42_contract.channels import ChannelNotification

    notification = ChannelNotification(message="tell us your size", schema=_form_schema())
    assert notification.schema == _form_schema()
    # Absent by default — a plain notification carries no form.
    assert ChannelNotification(message="hi").schema is None


def test_notification_carries_form_prefill_data_and_pages():
    # An ask-less form can open ALREADY FILLED IN: per-send prefill and a stepped-page
    # layout ride the same form send as its schema (the same the ask-path delivery keeps).
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import FormData, FormPage

    notification = ChannelNotification(
        message="tell us your size",
        schema=_form_schema(),
        data=FormData(values={"size": "M"}),
        pages=[FormPage(title="Details", fields=["size"])],
    )
    assert notification.data is not None
    assert notification.data.values == {"size": "M"}
    assert notification.pages is not None
    assert notification.pages[0].fields == ["size"]


def test_notification_data_and_pages_ride_a_form_send_only():
    # ``data``/``pages`` mean nothing without a schema; a non-form send refuses them loudly.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import FormData, FormPage

    with pytest.raises(ValidationError, match=r"no schema carries no form data"):
        ChannelNotification(message="hi", data=FormData(values={"size": "M"}))
    with pytest.raises(ValidationError, match=r"no schema carries no form pages"):
        ChannelNotification(message="hi", pages=[FormPage(title="Details", fields=["size"])])


def test_notification_schema_rejects_present_but_empty_dict():
    # A present schema is a non-empty dict — the same bound the ask-path delivery enforces;
    # the deep shape is the sender's shared subset walk, not this model's concern.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"non-empty dict"):
        ChannelNotification(message="hi", schema={})


def test_notification_schema_may_combine_with_media():
    from tai42_contract.channels import ChannelNotification

    notification = ChannelNotification(message="pick from the chart", media=[_image_item()], schema=_form_schema())
    assert notification.media == [_image_item()]
    assert notification.schema == _form_schema()


def test_notification_carries_a_form_tag_on_a_form_send():
    from tai42_contract.channels import ChannelNotification

    notification = ChannelNotification(message="tell us your size", schema=_form_schema(), form_tag="ref-42")
    assert notification.form_tag == "ref-42"
    # Absent by default, even on a form send.
    assert ChannelNotification(message="hi", schema=_form_schema()).form_tag is None


def test_notification_form_tag_rides_a_form_send_only():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"no schema carries no form tag"):
        ChannelNotification(message="hi", form_tag="ref-42")


def test_notification_form_tag_is_bounded():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"form_tag must be 1 to"):
        ChannelNotification(message="hi", schema=_form_schema(), form_tag="has:colon")


def test_notification_carries_a_wording_form_chapter_on_a_form_send():
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions import Wording

    notification = ChannelNotification(
        message="tell us about you",
        schema=_form_schema(),
        wording=Wording.model_validate({"form": {"open": "Fill it in", "cancel": "Never mind", "back": "Go back"}}),
    )
    assert notification.wording is not None
    assert notification.wording.form is not None
    assert notification.wording.form.cancel == "Never mind"


def test_notification_wording_form_chapter_rides_a_form_send_only():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions import Wording

    # No form surface: the form chapter names a form that is not there.
    with pytest.raises(
        ValidationError, match=r"a notification with no choice or form surface draws no wording.form.open"
    ):
        ChannelNotification(message="hi", wording=Wording.model_validate({"form": {"open": "Go"}}))


def test_notification_wording_member_must_be_non_blank():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions import Wording

    with pytest.raises(ValidationError, match=r"wording.form.title must be non-blank when set"):
        ChannelNotification(
            message="hi", schema=_form_schema(), wording=Wording.model_validate({"form": {"title": "   "}})
        )


def test_notification_carries_a_wording_choice_label_beside_its_options():
    from tai42_contract.channels import ChannelNotification, ReplyOption
    from tai42_contract.interactions import Wording

    notification = ChannelNotification(
        message="pick one",
        options=[ReplyOption(text="Item A")],
        wording=Wording.model_validate({"choice": {"label": "Open the list"}}),
    )
    assert notification.wording is not None
    assert notification.wording.choice is not None
    assert notification.wording.choice.label == "Open the list"


def test_notification_wording_choice_requires_a_choice_surface():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions import Wording

    # No choice surface: the label names a control that opens nothing.
    with pytest.raises(
        ValidationError, match=r"a notification with no choice or form surface draws no wording.choice.label"
    ):
        ChannelNotification(message="hi", wording=Wording.model_validate({"choice": {"label": "Open the list"}}))
    # A form carries no choice list, so a choice label beside a schema is refused too.
    with pytest.raises(ValidationError, match=r"notify:form draws no wording.choice.label"):
        ChannelNotification(
            message="hi", schema=_form_schema(), wording=Wording.model_validate({"choice": {"label": "Open the list"}})
        )


def test_notification_wording_choice_label_must_be_non_blank():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ReplyOption
    from tai42_contract.interactions import Wording

    with pytest.raises(ValidationError, match=r"wording.choice.label must be non-blank when set"):
        ChannelNotification(
            message="pick one",
            options=[ReplyOption(text="Item A")],
            wording=Wording.model_validate({"choice": {"label": "  "}}),
        )
