"""Tests for ``ChannelTemplate`` — the pre-approved out-of-window template send."""

from __future__ import annotations

import pytest


def test_template_roundtrips_on_a_notification():
    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    template = ChannelTemplate(name="status_update", language="en_US", body_parameters=["A-42", "done"])
    notification = ChannelNotification(message="Your item is done", template=template)
    assert notification.template is not None
    assert notification.template == template
    assert notification.template.name == "status_update"
    assert notification.template.language == "en_US"
    assert notification.template.body_parameters == ["A-42", "done"]
    # body_parameters is optional — a template with no body placeholders is valid, and header
    # media / button parameters default to empty.
    empty = ChannelTemplate(name="ping", language="en")
    assert empty.body_parameters == []
    assert empty.buttons == []
    assert empty.header_media is None


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
