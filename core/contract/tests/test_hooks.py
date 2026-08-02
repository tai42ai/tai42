"""Tests for the hooks contract models.

Pins the ``TopicVerifierBinding`` shape a hooks manager persists per topic: a
required ``verifier`` name, an optional ``config`` defaulting to ``{}``, frozen
after construction, and a loud rejection of a wrong shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.hooks import HookRegister, TopicVerifierBinding


def _valid_register(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "on-order",
        "topic": "orders",
        "tool": "notify",
        "execution_key": "k-fire",
    }
    body.update(overrides)
    return body


def test_binding_defaults_config_to_empty_dict():
    binding = TopicVerifierBinding(verifier="github")
    assert binding.verifier == "github"
    assert binding.config == {}


def test_binding_round_trips_through_json():
    binding = TopicVerifierBinding(verifier="shared_secret", config={"secret_env": "WH"})
    assert TopicVerifierBinding.model_validate_json(binding.model_dump_json()) == binding


def test_binding_is_frozen():
    binding = TopicVerifierBinding(verifier="github")
    with pytest.raises(ValidationError):
        binding.verifier = "other"  # type: ignore[misc]


def test_binding_rejects_missing_verifier():
    with pytest.raises(ValidationError):
        TopicVerifierBinding.model_validate({"config": {}})


def test_binding_rejects_empty_verifier():
    # ``verifier`` carries ``min_length=1``, so an empty name is rejected at the
    # model — every store/read path that validates a binding enforces this.
    with pytest.raises(ValidationError):
        TopicVerifierBinding(verifier="")
    with pytest.raises(ValidationError):
        TopicVerifierBinding.model_validate({"verifier": "", "config": {}})


def test_binding_rejects_non_dict_config():
    with pytest.raises(ValidationError):
        TopicVerifierBinding.model_validate({"verifier": "github", "config": "nope"})


@pytest.mark.parametrize("value", ["orders", "on-order", "hook_42", "t1", "a", "0abc"])
def test_register_accepts_url_safe_name_and_topic(value: str):
    # A single lowercase URL path segment (letters/digits leading, then ``-``/``_``)
    # is the routable shape, so it is accepted for both name and topic.
    assert HookRegister.model_validate(_valid_register(name=value)).name == value
    assert HookRegister.model_validate(_valid_register(topic=value)).topic == value


@pytest.mark.parametrize("field", ["name", "topic"])
@pytest.mark.parametrize(
    "value",
    [
        "a/b",  # the path separator: a topic with ``/`` never routes via the webhook URL
        "orders/",  # trailing separator
        "Orders",  # uppercase is outside the segment charset
        "on order",  # whitespace
        "topic.name",  # dot
        "café",  # non-ascii
        "-lead",  # must lead with a letter or digit
        "_lead",  # must lead with a letter or digit
        "good\n",  # a trailing newline cannot slip past the ``\Z`` anchor
    ],
)
def test_register_rejects_bad_charset_and_names_the_rule(field: str, value: str):
    with pytest.raises(ValidationError) as excinfo:
        HookRegister.model_validate(_valid_register(**{field: value}))
    message = str(excinfo.value)
    # The 400 the router/operation surfaces wraps this message, so the rule is named
    # to the caller: the failing field, the offending value, and the pattern.
    assert field in message
    assert "one lowercase URL path segment" in message


def test_stored_params_inherit_the_charset_rule():
    # ``HookParams`` (the persisted shape, incl. the backup-restore path) subclasses
    # ``HookRegister``, so a restore of a record with an unroutable topic is rejected
    # at the model just like a fresh registration.
    from tai42_contract.hooks import HookParams

    with pytest.raises(ValidationError):
        HookParams.model_validate(_valid_register(topic="a/b", execution_key_fingerprint="fp-fire"))
