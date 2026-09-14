"""Tests for ``TargetConversationConfig`` (the greeting-template rules) and the pairing errors."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_pairing_errors_are_distinct_named_types():
    from tai42_contract.conversations import (
        CrossTargetMergeError,
        MultichannelDisabledError,
        NotLinkedError,
        PairCodeInvalidError,
    )

    for exc in (CrossTargetMergeError, MultichannelDisabledError, NotLinkedError, PairCodeInvalidError):
        assert issubclass(exc, Exception)
    # Each is its own type — a pairing turn scopes them apart from one another and from infra.
    assert len({CrossTargetMergeError, MultichannelDisabledError, NotLinkedError, PairCodeInvalidError}) == 4


def test_target_config_defaults_and_frozen():
    from tai42_contract.conversations import TargetConversationConfig

    config = TargetConversationConfig(target_kind="agent", target_name="assistant")
    assert config.multichannel is False
    assert config.greeting_template is None
    with pytest.raises(ValidationError):
        config.multichannel = True  # type: ignore[misc]


def test_target_config_accepts_the_pairing_code_placeholder():
    from tai42_contract.conversations import TargetConversationConfig

    config = TargetConversationConfig(
        target_kind="tool", target_name="lookup", greeting_template="Welcome! Pair with {pairing_code}."
    )
    assert config.greeting_template == "Welcome! Pair with {pairing_code}."


def test_target_config_allows_escaped_braces():
    from tai42_contract.conversations import TargetConversationConfig

    # ``{{`` / ``}}`` are literal braces, not a placeholder, so they are allowed.
    config = TargetConversationConfig(target_kind="agent", target_name="c", greeting_template="use {{curly}} here")
    assert config.greeting_template == "use {{curly}} here"


@pytest.mark.parametrize(
    "template",
    [
        "hi {name}",  # a foreign field
        "hi {}",  # an auto-numbered field
        "hi {pairing_code!r}",  # a conversion
        "hi {pairing_code:>10}",  # a format spec
        "hi {pairing_code.attr}",  # an attribute access
        "hi {unbalanced",  # a malformed template
    ],
)
def test_target_config_refuses_a_disallowed_greeting_template(template: str):
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="agent", target_name="c", greeting_template=template)


def test_target_config_refuses_a_blank_greeting_template():
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="agent", target_name="c", greeting_template="   ")


def test_target_config_refuses_a_blank_target_name():
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="agent", target_name="   ")


def test_target_config_refuses_an_unknown_target_kind():
    from tai42_contract.conversations import TargetConversationConfig

    with pytest.raises(ValidationError):
        TargetConversationConfig(target_kind="robot", target_name="c")  # type: ignore[arg-type]
