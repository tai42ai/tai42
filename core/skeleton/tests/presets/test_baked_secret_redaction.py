"""The preset read-view secret redaction: a value baked under a secret-typed leaf of
the base tool's input schema is masked on a read, markers and non-secret leaves are
kept, and — with no schema available — every leaf fails closed.

These oracles drive the pure schema walk directly with hand-built JSON schemas (the
shape pydantic emits for ``SecretStr`` — ``{"type": "string", "format": "password",
"writeOnly": true}``), so the masking rule is pinned independently of any tool.
"""

from __future__ import annotations

from tai42_contract.secrets import SECRET_PLACEHOLDER

from tai42_skeleton.presets.secret_refs import mask_all_baked_secrets, redact_baked_secrets

_SECRET_LEAF = {"type": "string", "format": "password", "writeOnly": True}
_MARKER = "!ENV ${VAULT_TOKEN}"


def test_masks_a_baked_secret_scalar_leaf_and_keeps_a_non_secret_one() -> None:
    schema = {"properties": {"token": _SECRET_LEAF, "label": {"type": "string"}}}
    out = redact_baked_secrets({"token": "s3kr3t", "label": "public"}, schema)
    assert out == {"token": SECRET_PLACEHOLDER, "label": "public"}


def test_keeps_an_env_marker_under_a_secret_leaf() -> None:
    # A marker is a reference the store keeps, not a baked credential — masking it
    # would hide nothing and lose the reference.
    schema = {"properties": {"token": _SECRET_LEAF}}
    assert redact_baked_secrets({"token": _MARKER}, schema) == {"token": _MARKER}


def test_masks_a_secret_branch_of_an_optional_union() -> None:
    # ``str | None`` over a ``SecretStr`` — a single secret branch is enough to mask.
    schema = {"properties": {"token": {"anyOf": [_SECRET_LEAF, {"type": "null"}]}}}
    assert redact_baked_secrets({"token": "s3kr3t"}, schema) == {"token": SECRET_PLACEHOLDER}


def test_masks_secret_leaves_nested_in_objects_and_arrays() -> None:
    schema = {
        "properties": {
            "creds": {
                "type": "object",
                "properties": {"api_key": _SECRET_LEAF, "name": {"type": "string"}},
            },
            "keys": {"type": "array", "items": _SECRET_LEAF},
        }
    }
    baked = {"creds": {"api_key": "abc", "name": "prod"}, "keys": ["k1", "k2"]}
    out = redact_baked_secrets(baked, schema)
    assert out == {
        "creds": {"api_key": SECRET_PLACEHOLDER, "name": "prod"},
        "keys": [SECRET_PLACEHOLDER, SECRET_PLACEHOLDER],
    }


def test_leaves_everything_under_a_permissive_leaf_untouched() -> None:
    # An ``Any`` / untyped leaf carries no secret typing to key off, so a value under
    # it is not masked (the same limit the reveal walk has).
    schema = {"properties": {"blob": {}}}
    assert redact_baked_secrets({"blob": "whatever"}, schema) == {"blob": "whatever"}


def test_schema_without_properties_leaves_values_untouched() -> None:
    # A base schema carrying no ``properties`` (or a non-mapping one) types nothing, so
    # no leaf is masked.
    assert redact_baked_secrets({"a": "b"}, {}) == {"a": "b"}


def test_container_under_a_non_matching_schema_is_left_untouched() -> None:
    # A dict/list value whose schema types it as a (non-secret) scalar has no object or
    # array branch to descend, so it passes through unchanged.
    schema = {"properties": {"obj": {"type": "string"}, "arr": {"type": "string"}}}
    baked = {"obj": {"k": "v"}, "arr": ["x"]}
    assert redact_baked_secrets(baked, schema) == baked


def test_never_mutates_the_input() -> None:
    schema = {"properties": {"token": _SECRET_LEAF}}
    baked = {"token": "s3kr3t"}
    redact_baked_secrets(baked, schema)
    assert baked == {"token": "s3kr3t"}


def test_mask_all_baked_secrets_masks_every_leaf_but_keeps_markers() -> None:
    # The fail-closed fallback when the base tool's schema is unavailable: every leaf
    # is masked (a config leaf included), an ``!ENV`` marker is kept.
    baked = {"token": "s3kr3t", "label": "public", "ref": _MARKER, "nested": {"n": 1}, "list": ["a"]}
    assert mask_all_baked_secrets(baked) == {
        "token": SECRET_PLACEHOLDER,
        "label": SECRET_PLACEHOLDER,
        "ref": _MARKER,
        "nested": {"n": SECRET_PLACEHOLDER},
        "list": [SECRET_PLACEHOLDER],
    }
