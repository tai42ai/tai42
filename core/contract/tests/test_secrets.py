"""Contract tests for the secret-value envelope.

Pin the disclosure guards (repr/str + non-serializability), the ``mask_secrets``
and ``unwrap_secrets`` walks (placement, sibling and original-object safety,
mask-loses-the-secret), and ``reveal`` identity.
"""

from __future__ import annotations

import json
import pickle
from typing import Any

import pytest


def test_repr_and_str_never_show_the_value():
    from tai42_contract.secrets import SecretValue

    secret = SecretValue("tok-4242-xyzzy")
    assert repr(secret) == "SecretValue([secret])"
    assert str(secret) == "SecretValue([secret])"
    assert "tok-4242-xyzzy" not in repr(secret)
    assert "tok-4242-xyzzy" not in str(secret)


def test_not_json_serializable():
    from tai42_contract.secrets import SecretValue

    with pytest.raises(TypeError):
        json.dumps(SecretValue("tok-4242-xyzzy"))
    with pytest.raises(TypeError):
        json.dumps({"a": SecretValue("tok-4242-xyzzy")})


def test_not_pickle_serializable():
    from tai42_contract.secrets import SecretValue

    with pytest.raises(TypeError):
        pickle.dumps(SecretValue("tok-4242-xyzzy"))
    with pytest.raises(TypeError):
        pickle.dumps({"a": SecretValue("tok-4242-xyzzy")})


def test_pickle_refusal_message_carries_no_secret():
    from tai42_contract.secrets import SecretValue

    with pytest.raises(TypeError) as excinfo:
        pickle.dumps(SecretValue("tok-4242-xyzzy"))
    assert "tok-4242-xyzzy" not in str(excinfo.value)


def test_mask_secrets_places_placeholder_and_leaves_original_intact():
    from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue, mask_secrets

    original: dict[str, Any] = {
        "token": SecretValue("tok-4242-xyzzy"),
        "name": "alice",
        "nested": [1, SecretValue("pw-9001-plugh"), "keep"],
    }
    masked = mask_secrets(original)

    assert masked["token"] == SECRET_PLACEHOLDER
    assert masked["nested"][1] == SECRET_PLACEHOLDER
    assert masked["name"] == "alice"
    assert masked["nested"][0] == 1
    assert masked["nested"][2] == "keep"

    assert isinstance(original["token"], SecretValue)
    assert isinstance(original["nested"][1], SecretValue)

    dumped = json.dumps(masked)
    assert "tok-4242-xyzzy" not in dumped
    assert "pw-9001-plugh" not in dumped


def test_unwrap_secrets_restores_real_values():
    from tai42_contract.secrets import SecretValue, unwrap_secrets

    original = {
        "token": SecretValue("tok-4242-xyzzy"),
        "name": "alice",
        "nested": [1, SecretValue("pw-9001-plugh"), "keep"],
    }
    unwrapped = unwrap_secrets(original)

    assert unwrapped["token"] == "tok-4242-xyzzy"
    assert unwrapped["nested"][1] == "pw-9001-plugh"
    assert unwrapped["name"] == "alice"
    assert isinstance(original["token"], SecretValue)


def test_mask_then_unwrap_does_not_resurrect_secret():
    from tai42_contract.secrets import SecretValue, mask_secrets, unwrap_secrets

    original = {"token": SecretValue("tok-4242-xyzzy")}
    round_tripped = unwrap_secrets(mask_secrets(original))

    assert round_tripped["token"] != "tok-4242-xyzzy"
    assert "tok-4242-xyzzy" not in json.dumps(round_tripped)


def test_reveal_returns_the_exact_object():
    from tai42_contract.secrets import SecretValue

    payload = {"nested": "tok-4242-xyzzy"}
    secret = SecretValue(payload)
    assert secret.reveal() is payload


def test_contains_secrets_true_for_a_bare_secret_and_nested_leaves():
    from tai42_contract.secrets import SecretValue, contains_secrets

    assert contains_secrets(SecretValue("tok-4242-xyzzy")) is True
    assert contains_secrets({"token": SecretValue("tok-4242-xyzzy")}) is True
    assert contains_secrets([1, {"a": SecretValue("pw-9001-plugh")}]) is True
    assert contains_secrets((0, [SecretValue("tok-4242-xyzzy")])) is True


def test_contains_secrets_false_for_secret_free_structures():
    from tai42_contract.secrets import contains_secrets

    assert contains_secrets("alice") is False
    assert contains_secrets({"token": "[secret]", "nested": [1, "keep"]}) is False
    assert contains_secrets([]) is False
    assert contains_secrets(None) is False
