"""The one key check every storage and template operation runs on a caller's key."""

from __future__ import annotations

import pytest

from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations.store_keys import checked_store_key


def _message(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


@pytest.mark.parametrize("key", ["a/b.txt", "x", "inbound-media/2026/a.png"])
def test_a_canonical_key_is_returned(key: str) -> None:
    assert checked_store_key(key) == key


@pytest.mark.parametrize("key", ["./a", "a/../b", "a//b", "a/", "/a", ".", "a/..", "x/.", "../x"])
def test_a_non_canonical_key_is_a_bad_request_naming_it(key: str) -> None:
    with pytest.raises(BadRequestError) as caught:
        checked_store_key(key)
    assert str(caught.value) == _message(key)


def test_a_backslash_key_keeps_the_unsafe_path_message() -> None:
    with pytest.raises(BadRequestError) as caught:
        checked_store_key("a\\b")
    assert str(caught.value) == "unsafe template path: 'a\\\\b'"


@pytest.mark.parametrize("key", ["", None, 5])
def test_an_empty_or_non_string_key_is_a_bad_request(key: object) -> None:
    with pytest.raises(BadRequestError, match="non-empty string"):
        checked_store_key(key)
