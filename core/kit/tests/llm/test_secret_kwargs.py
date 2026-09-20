"""KwargsCacheKey / unwrap_secret_kwargs: nested-secret masking and unwrapping.

The cache key must never carry a plaintext secret, must keep distinct secrets
distinct (even nested inside containers), and must not crash on a
non-serializable kwarg. Unwrapping must reveal secrets at any depth for the
constructor.
"""

import pytest

pytest.importorskip("langgraph")

from pydantic import SecretStr

from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs


def test_nested_secret_not_in_key_plaintext():
    key = KwargsCacheKey({"opts": {"api_key": SecretStr("sk-secret")}})._key
    assert "sk-secret" not in key
    assert "sha256:" in key


def test_nested_distinct_secrets_split_the_key():
    a = KwargsCacheKey({"opts": {"api_key": SecretStr("sk-a")}})
    b = KwargsCacheKey({"opts": {"api_key": SecretStr("sk-b")}})
    c = KwargsCacheKey({"opts": {"api_key": SecretStr("sk-a")}})
    # A value-blind SecretStr repr would collide sk-a and sk-b; the digest keeps
    # them distinct while equal secrets still collapse to one entry.
    assert a != b
    assert a == c
    assert hash(a) == hash(c)


def test_secret_in_list_masked():
    key = KwargsCacheKey({"keys": [SecretStr("sk-x"), "plain"]})._key
    assert "sk-x" not in key
    assert "plain" in key


def test_non_serializable_top_level_kwarg_does_not_crash():
    # A non-serializable leaf is fingerprinted via repr rather than raising.
    sentinel = object()
    key = KwargsCacheKey({"model": sentinel})._key
    assert isinstance(key, str)


def test_unwrap_reveals_nested_secret_plaintext():
    out = unwrap_secret_kwargs({"opts": {"api_key": SecretStr("sk-nested")}, "stop": ("a", "b")})
    assert out["opts"]["api_key"] == "sk-nested"
    # Non-secret containers keep their shape (a tuple stays a tuple).
    assert out["stop"] == ("a", "b")
    assert isinstance(out["stop"], tuple)


def test_unwrap_reveals_secret_in_list():
    out = unwrap_secret_kwargs({"keys": [SecretStr("sk-1"), "plain"]})
    assert out["keys"] == ["sk-1", "plain"]
