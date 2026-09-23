"""The two bind steps for ``!ENV ${VAR}`` secret references in a preset's ``fixed_kwargs``.

``resolve_secret_refs`` materialises each reference: a present variable resolves to a
masked ``SecretValue``; an absent variable with a ``:default`` resolves to that default
in the clear; an absent required variable — or a marker that is not a single
``${VAR[:default]}`` reference — raises loudly naming the variable and the leaf.
Non-marker leaves and non-string values pass through untouched, and the input is never
mutated.

``reveal_typed_scalar_refs`` then reveals a top-level resolved value into its plain form
exactly when its base-tool parameter declares a concrete type, so a ``token: str`` (or
``str | None``) parameter receives the resolved string; a value baked into a permissive
parameter, or nested inside a container value, stays wrapped.
"""

from __future__ import annotations

import pytest
from tai42_contract.secrets import SecretValue

from tai42_skeleton.presets.secret_refs import resolve_secret_refs, reveal_typed_scalar_refs


def test_present_var_resolves_to_a_masked_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_TOKEN", "s3cr3t")
    resolved = resolve_secret_refs({"token": "!ENV ${PRESET_TOKEN}"})
    secret = resolved["token"]
    assert isinstance(secret, SecretValue)
    assert secret.reveal() == "s3cr3t"


def test_absent_required_var_raises_naming_var_and_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_MISSING", raising=False)
    with pytest.raises(ValueError, match=r"PRESET_MISSING") as exc:
        resolve_secret_refs({"token": "!ENV ${PRESET_MISSING}"})
    assert "/token" in str(exc.value)


def test_absent_var_with_default_resolves_to_the_default_in_the_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_REGION", raising=False)
    resolved = resolve_secret_refs({"region": "!ENV ${PRESET_REGION:eu-west}"})
    # A default is opt-in non-secret config: stored/baked in the clear, never wrapped.
    assert resolved["region"] == "eu-west"
    assert not isinstance(resolved["region"], SecretValue)


def test_present_var_wins_over_its_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_REGION", "us-east")
    resolved = resolve_secret_refs({"region": "!ENV ${PRESET_REGION:eu-west}"})
    secret = resolved["region"]
    assert isinstance(secret, SecretValue)
    assert secret.reveal() == "us-east"


def test_nested_leaves_resolve_through_dicts_and_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_KEY", "abc")
    monkeypatch.delenv("PRESET_HOST", raising=False)
    resolved = resolve_secret_refs(
        {"headers": {"authorization": "!ENV ${PRESET_KEY}"}, "hosts": ["!ENV ${PRESET_HOST:localhost}", "plain"]}
    )
    assert isinstance(resolved["headers"]["authorization"], SecretValue)
    assert resolved["headers"]["authorization"].reveal() == "abc"
    assert resolved["hosts"] == ["localhost", "plain"]


def test_nested_absent_required_var_names_the_full_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_DEEP", raising=False)
    with pytest.raises(ValueError, match=r"PRESET_DEEP") as exc:
        resolve_secret_refs({"outer": {"inner": ["!ENV ${PRESET_DEEP}"]}})
    assert "/outer/inner/0" in str(exc.value)


def test_non_marker_and_non_string_values_pass_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_X", raising=False)
    original = {
        "plain": "just a string",
        "looks_like_ref": "${PRESET_X}",  # no !ENV prefix → not a marker
        "count": 7,
        "flag": True,
        "nothing": None,
        "nums": [1, 2.5],
    }
    resolved = resolve_secret_refs(original)
    assert resolved == original


def test_malformed_marker_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_A", "a")
    monkeypatch.setenv("PRESET_B", "b")
    # Surrounding text / more than one reference is not a single secret reference.
    with pytest.raises(ValueError, match=r"malformed"):
        resolve_secret_refs({"url": "!ENV https://${PRESET_A}/${PRESET_B}"})


def test_input_is_not_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_TOKEN", "s3cr3t")
    original = {"token": "!ENV ${PRESET_TOKEN}", "nested": {"k": "!ENV ${PRESET_TOKEN}"}}
    resolve_secret_refs(original)
    assert original["token"] == "!ENV ${PRESET_TOKEN}"
    assert original["nested"] == {"k": "!ENV ${PRESET_TOKEN}"}


# -- reveal_typed_scalar_refs: reveal a resolved value for a TYPED base parameter -------

_STR_PARAM = {"properties": {"token": {"type": "string"}}}
_OPTIONAL_STR_PARAM = {"properties": {"token": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
_PERMISSIVE_PARAM = {"properties": {"token": {}}}
_DICT_PARAM = {"properties": {"payload": {"type": "object", "additionalProperties": True}}}


def test_reveal_unwraps_a_secret_baked_into_a_typed_str_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_scalar_refs({"token": secret}, _STR_PARAM)
    # A ``str`` parameter would reject the wrapper at pydantic validation, so it is
    # revealed to its plain value.
    assert revealed == {"token": "s3cr3t"}
    assert not isinstance(revealed["token"], SecretValue)


def test_reveal_unwraps_a_secret_baked_into_an_optional_str_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_scalar_refs({"token": secret}, _OPTIONAL_STR_PARAM)
    assert revealed == {"token": "s3cr3t"}


def test_reveal_keeps_the_wrapper_for_a_permissive_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_scalar_refs({"token": secret}, _PERMISSIVE_PARAM)
    # A permissive (``Any`` / ``object``) parameter accepts the wrapper unchanged, so it
    # stays wrapped and is masked wherever the run is recorded.
    assert revealed["token"] is secret


def test_reveal_leaves_a_nested_secret_wrapped_under_a_container_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_scalar_refs({"payload": {"token": secret}}, _DICT_PARAM)
    # Only a TOP-LEVEL baked value is revealed; a wrapper nested inside a container value
    # survives (a container parameter validates its leaves loosely or not at all).
    assert revealed["payload"]["token"] is secret


def test_reveal_passes_non_secret_and_unknown_params_through() -> None:
    revealed = reveal_typed_scalar_refs({"token": "plain", "extra": 7}, _STR_PARAM)
    assert revealed == {"token": "plain", "extra": 7}


def test_reveal_does_not_mutate_the_input() -> None:
    secret = SecretValue("s3cr3t")
    original = {"token": secret}
    reveal_typed_scalar_refs(original, _STR_PARAM)
    assert original["token"] is secret
