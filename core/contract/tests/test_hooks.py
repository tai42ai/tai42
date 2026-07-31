"""Tests for the hooks contract models.

Pins the ``TopicVerifierBinding`` shape a hooks manager persists per topic: a
required ``verifier`` name, an optional ``config`` defaulting to ``{}``, frozen
after construction, and a loud rejection of a wrong shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.hooks import TopicVerifierBinding


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
