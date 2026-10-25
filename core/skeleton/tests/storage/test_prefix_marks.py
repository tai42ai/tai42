"""The prefix-mark registry: one canonical prefix rule for the not-backed-up and unlisted marks."""

from __future__ import annotations

import re

import pytest

from tai42_skeleton.storage.prefix_marks import PrefixMarkRegistry, PrefixSet, covering_prefix


@pytest.mark.parametrize("mark", ["not_backed_up", "unlisted"])
def test_declare_is_idempotent_and_listed(mark: str) -> None:
    registry = PrefixMarkRegistry()
    prefixes: PrefixSet = getattr(registry, mark)
    prefixes.declare("inbound-media/")
    prefixes.declare("inbound-media/")
    prefixes.declare("a/b/")
    assert prefixes.prefixes() == frozenset({"inbound-media/", "a/b/"})


@pytest.mark.parametrize("mark", ["not_backed_up", "unlisted"])
@pytest.mark.parametrize("bad", ["inbound-media", "/", "", "../x/", "./inbound-media/", "a//", "/a/"])
def test_a_non_canonical_prefix_is_refused_naming_it(mark: str, bad: str) -> None:
    prefixes: PrefixSet = getattr(PrefixMarkRegistry(), mark)
    with pytest.raises(ValueError, match=re.escape(repr(bad))):
        prefixes.declare(bad)
    assert prefixes.prefixes() == frozenset()


def test_a_non_string_prefix_is_refused() -> None:
    with pytest.raises(ValueError, match="prefix"):
        PrefixSet().declare(5)  # type: ignore[arg-type]


def test_the_marks_are_independent() -> None:
    registry = PrefixMarkRegistry()
    registry.unlisted.declare("hidden/")
    registry.not_backed_up.declare("volatile/")
    assert registry.unlisted.prefixes() == frozenset({"hidden/"})
    assert registry.not_backed_up.prefixes() == frozenset({"volatile/"})


def test_reset_empties_both_marks() -> None:
    registry = PrefixMarkRegistry()
    registry.unlisted.declare("hidden/")
    registry.not_backed_up.declare("volatile/")
    registry.reset()
    assert registry.unlisted.prefixes() == frozenset()
    assert registry.not_backed_up.prefixes() == frozenset()


def test_covering_prefix_answers_the_prefix_a_key_starts_with() -> None:
    marks = frozenset({"inbound-media/", "a/b/"})
    assert covering_prefix("inbound-media/x", marks) == "inbound-media/"
    assert covering_prefix("a/b/c/d", marks) == "a/b/"
    assert covering_prefix("inbound-media", marks) is None
    assert covering_prefix("inbound-mediax/y", marks) is None
    assert covering_prefix("notes/b.txt", marks) is None
    assert covering_prefix("anything", frozenset()) is None
