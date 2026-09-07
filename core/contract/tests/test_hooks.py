"""Tests for the hooks contract models.

Pins the ``TopicVerifierBinding`` shape a hooks manager persists per topic: a
required ``verifier`` name, an optional ``config`` defaulting to ``{}``, frozen
after construction, and a loud rejection of a wrong shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.hooks import HookRegister, HookSubject, TopicVerifierBinding


def _valid_register(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "on-event",
        "topic": "events",
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


@pytest.mark.parametrize("value", ["events", "on-event", "hook_42", "t1", "a", "0abc"])
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
        "events/",  # trailing separator
        "Events",  # uppercase is outside the segment charset
        "on event",  # whitespace
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


def test_register_defaults_subject_to_none():
    # A hook with no Subject group carries no ambient state context at the fire.
    assert HookRegister.model_validate(_valid_register()).subject is None


def test_register_accepts_a_subject_group():
    reg = HookRegister.model_validate(
        _valid_register(
            subject={"target_kind": "tool", "target_name": "assistant", "kind": "thread", "key_expr": ".id"}
        )
    )
    assert reg.subject is not None
    assert reg.subject.key_expr == ".id"


def test_hook_subject_is_frozen_and_validates_its_shape():
    subject = HookSubject(target_kind="tool", target_name="assistant", kind="person", key_expr=".actor")
    with pytest.raises(ValidationError):
        subject.kind = "other"  # type: ignore[misc]
    # A blank key_expr, a bad kind charset, and an extra key are each refused.
    with pytest.raises(ValidationError):
        HookSubject(target_kind="tool", target_name="assistant", kind="person", key_expr="")
    with pytest.raises(ValidationError):
        HookSubject(target_kind="tool", target_name="assistant", kind="Person", key_expr=".actor")
    with pytest.raises(ValidationError):
        HookSubject.model_validate(
            {"target_kind": "tool", "target_name": "a", "kind": "person", "key_expr": ".x", "bogus": 1}
        )
