"""The shared named-error helpers: the fixed message shape, the value/secret
guards, and the fail-closed treatment of an empty secret."""

import pytest
from pydantic import SecretStr

from tai42_kit.settings import not_configured_message, require, require_secret


def test_message_without_default_names_only_the_specific_var():
    assert not_configured_message("the widget", "WIDGET_URL") == "the widget is not configured: set WIDGET_URL."


def test_message_with_default_names_the_shared_alternative():
    message = not_configured_message("the widget", "WIDGET_REDIS_URL", "TAI_DEFAULT_REDIS_URL")
    assert message == "the widget is not configured: set WIDGET_REDIS_URL (or TAI_DEFAULT_REDIS_URL)."


def test_require_returns_a_configured_value():
    assert require("here", "the widget", "WIDGET_URL") == "here"


def test_require_raises_the_named_error_on_none():
    with pytest.raises(ValueError, match=r"the widget is not configured: set WIDGET_URL \(or TAI_DEFAULT_URL\)\."):
        require(None, "the widget", "WIDGET_URL", "TAI_DEFAULT_URL")


def test_require_secret_returns_the_plaintext():
    assert require_secret(SecretStr("s3cr3t"), "the widget", "WIDGET_TOKEN") == "s3cr3t"


def test_require_secret_raises_on_unset():
    with pytest.raises(ValueError, match=r"the widget is not configured: set WIDGET_TOKEN\."):
        require_secret(None, "the widget", "WIDGET_TOKEN")


def test_require_secret_fails_closed_on_empty():
    # An empty secret must not satisfy the gate — fail CLOSED, naming the var.
    with pytest.raises(ValueError, match="WIDGET_TOKEN is set but empty"):
        require_secret(SecretStr(""), "the widget", "WIDGET_TOKEN")
