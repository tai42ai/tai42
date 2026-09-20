"""Tests for the interactive option shapes and their list-level caps, exercised through
the option-carrying ``ChannelNotification``."""

from __future__ import annotations

from typing import Any

import pytest


def _image_item() -> Any:
    from tai42_contract.interactions.models import MediaItem, MediaKind

    return MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/product.jpg", caption="A product")


def test_notification_accepts_options():
    from tai42_contract.channels import ChannelNotification, LinkOption, ReplyOption

    notification = ChannelNotification(
        message="pick one", options=[ReplyOption(text="Item A"), LinkOption(label="Docs", url="https://x.example/d")]
    )
    assert notification.options == [ReplyOption(text="Item A"), LinkOption(label="Docs", url="https://x.example/d")]
    # Absent by default; freeform text needs no options.
    assert ChannelNotification(message="hi").options is None


def test_notification_options_rejects_present_but_empty_list():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="non-empty list"):
        ChannelNotification(message="hi", options=[])


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_notification_options_rejects_a_blank_option(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ReplyOption

    with pytest.raises(ValidationError, match="reply option text must be non-blank"):
        ChannelNotification(message="hi", options=[ReplyOption(text="Item A"), ReplyOption(text=blank)])


def test_notification_options_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_OPTIONS_MAX, ChannelNotification, Option, ReplyOption

    ok: list[Option] = [ReplyOption(text=f"Item {n}") for n in range(NOTIFICATION_OPTIONS_MAX)]
    assert ChannelNotification(message="hi", options=ok).options == ok
    with pytest.raises(ValidationError, match=f"options carries at most {NOTIFICATION_OPTIONS_MAX} entries"):
        ChannelNotification(message="hi", options=[*ok, ReplyOption(text="one too many")])


def test_notification_option_length_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_OPTION_MAX_CHARS, ChannelNotification, ReplyOption

    at_cap = "x" * NOTIFICATION_OPTION_MAX_CHARS
    assert ChannelNotification(message="hi", options=[ReplyOption(text=at_cap)]).options == [ReplyOption(text=at_cap)]
    with pytest.raises(ValidationError, match="at most"):
        ChannelNotification(message="hi", options=[ReplyOption(text="x" * (NOTIFICATION_OPTION_MAX_CHARS + 1))])


def test_notification_options_may_combine_with_media():
    from tai42_contract.channels import ChannelNotification, ReplyOption

    notification = ChannelNotification(
        message="a card with a list", media=[_image_item()], options=[ReplyOption(text="Item A")]
    )
    assert notification.media == [_image_item()]
    assert notification.options == [ReplyOption(text="Item A")]
