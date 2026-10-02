"""Tests for ``ChannelTemplate`` — the generic pre-approved out-of-window template reference."""

from __future__ import annotations

import pytest


def test_template_roundtrips_on_a_notification():
    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    template = ChannelTemplate(name="status_update", language="en_US", parameters={"body": ["A-42", "done"]})
    notification = ChannelNotification(message="Your item is done", template=template)
    assert notification.template is not None
    assert notification.template == template
    assert notification.template.name == "status_update"
    assert notification.template.language == "en_US"
    # ``parameters`` is an opaque object the platform threads unopened.
    assert notification.template.parameters == {"body": ["A-42", "done"]}
    # ``parameters`` is optional — a template with no runtime arguments carries none.
    empty = ChannelTemplate(name="ping", language="en")
    assert empty.parameters is None


def test_template_parameters_stay_opaque():
    # The contract models ``parameters`` as an arbitrary object; it never reaches inside for a
    # channel-specific key, so any channel's own parameter shape rides as-is.
    from tai42_contract.channels import ChannelTemplate

    template = ChannelTemplate(
        name="promo",
        language="pt_BR",
        parameters={"anything": {"a channel owns": True}, "nested": [1, 2, 3]},
    )
    assert template.parameters == {"anything": {"a channel owns": True}, "nested": [1, 2, 3]}


def test_template_rejects_empty_parameters():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelTemplate

    with pytest.raises(ValidationError, match="non-empty dict"):
        ChannelTemplate(name="status_update", language="en_US", parameters={})


def test_template_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelTemplate

    template = ChannelTemplate(name="status_update", language="en_US")
    with pytest.raises(ValidationError):
        template.name = "changed"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_template_rejects_blank_name(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelTemplate

    with pytest.raises(ValidationError, match="non-blank"):
        ChannelTemplate(name=blank, language="en_US")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_template_rejects_blank_language(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelTemplate

    with pytest.raises(ValidationError, match="non-blank"):
        ChannelTemplate(name="status_update", language=blank)
