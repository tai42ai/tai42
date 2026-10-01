"""The ask-time reaction TRANSPORT gate: a channel-delivered reacting form needs
``supports_form_reaction``; a ``channel=None`` reacting form is served by the in-app door."""

from __future__ import annotations

from typing import cast

import pytest
from tai42_contract.channels import Channel
from tai42_contract.interactions import AnswerFormat

from tai42_skeleton.interactions.ask.validate import _validate_channel_args, _validate_channel_form


class _FormChannel:
    supports_form_delivery = True


class _ReactingChannel:
    supports_form_delivery = True
    supports_form_reaction = True


def test_reacting_form_refused_on_a_channel_without_the_flag():
    with pytest.raises(ValueError, match="does not support reacting forms"):
        _validate_channel_form("sms", cast("Channel", _FormChannel()), None, "q", "react_tool")


def test_reacting_form_allowed_on_a_channel_with_the_flag():
    assert _validate_channel_form("wa", cast("Channel", _ReactingChannel()), None, "q", "react_tool") is None


def test_static_form_allowed_on_a_delivery_only_channel():
    assert _validate_channel_form("wa", cast("Channel", _FormChannel()), None, "q", None) is None


def test_channel_none_reacting_form_is_allowed():
    # No channel: the in-app reaction door serves it, so there is no transport gate.
    channel_obj, schema = _validate_channel_args(
        None, None, None, AnswerFormat.FORM, {"type": "object"}, "q", None, "react_tool"
    )
    assert channel_obj is None
    assert schema == {"type": "object"}
