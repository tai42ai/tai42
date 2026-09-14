"""Tests for the shared interactive-composition rules, exercised through ``ChannelNotification``:
the media-only blank-message matrix and the surface/template exclusivity rules."""

from __future__ import annotations

from typing import Any

import pytest


def _image_item() -> Any:
    from tai42_contract.interactions.models import MediaItem, MediaKind

    return MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/product.jpg", caption="A product")


def _form_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"size": {"type": "string"}}}


def test_notification_media_and_template_are_mutually_exclusive():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    with pytest.raises(ValidationError, match="mutually exclusive"):
        ChannelNotification(
            message="both set",
            media=[_image_item()],
            template=ChannelTemplate(name="status_update", language="en_US"),
        )


def test_notification_options_and_template_are_mutually_exclusive():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ChannelTemplate, ReplyOption

    with pytest.raises(ValidationError, match="mutually exclusive"):
        ChannelNotification(
            message="both set",
            options=[ReplyOption(text="Item A")],
            template=ChannelTemplate(name="status_update", language="en_US"),
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_notification_schema_requires_a_non_blank_message(blank: str):
    # A form needs a prompt: a media-only (blank-message) send carries no schema.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match=r"carries no schema; a form needs a prompt"):
        ChannelNotification(message=blank, media=[_image_item()], schema=_form_schema())


def test_notification_schema_and_template_are_mutually_exclusive():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    with pytest.raises(ValidationError, match="mutually exclusive"):
        ChannelNotification(
            message="both set",
            schema=_form_schema(),
            template=ChannelTemplate(name="status_update", language="en_US"),
        )


def test_notification_schema_and_options_are_mutually_exclusive():
    # One message carries ONE interactive surface: a form's fields or a tap list, never both.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ReplyOption

    with pytest.raises(ValidationError, match="mutually exclusive"):
        ChannelNotification(message="both set", schema=_form_schema(), options=[ReplyOption(text="Item A")])


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_notification_media_only_message_may_be_blank(blank: str):
    # A caption-less image: a blank message is admissible when media carries it. ``message``
    # stays REQUIRED (constructed in code), so a media-only sender passes ``""`` explicitly.
    from tai42_contract.channels import ChannelNotification

    item = _image_item()
    assert ChannelNotification(message=blank, media=[item]).media == [item]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_notification_blank_message_without_media_is_refused(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="non-blank unless media"):
        ChannelNotification(message=blank)


def test_notification_media_only_carries_no_options():
    # Options require a non-blank message — a tappable choice needs a prompt.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ReplyOption

    with pytest.raises(ValidationError, match=r"content-only .* carries no options"):
        ChannelNotification(message="", media=[_image_item()], options=[ReplyOption(text="Item A")])


def test_notification_blank_message_with_only_template_is_refused():
    # A template is not media, so a blank message carrying only a template has no text carrier.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    with pytest.raises(ValidationError, match="non-blank unless media"):
        ChannelNotification(message="", template=ChannelTemplate(name="status_update", language="en_US"))
