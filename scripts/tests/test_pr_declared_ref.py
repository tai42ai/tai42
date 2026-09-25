"""Unit tests for scripts/pr_declared_ref.py — the resolver ci.yml uses to read
the paired-repo ref a pull request declares in its body. Hermetic: every case is
a synthetic body string, so nothing reads the environment or runs git."""

from __future__ import annotations

import pytest

import pr_declared_ref as pdr  # importable via the scripts/ path conftest.py injects

FIELD = "tai-studio-ref"


def test_absent_field_defaults_to_main() -> None:
    body = "feat: a change\n\nSome description with no ref line."
    assert pdr.resolve_ref(FIELD, body) == "main"


def test_empty_body_defaults_to_main() -> None:
    assert pdr.resolve_ref(FIELD, "") == "main"


def test_branch_value_is_returned() -> None:
    body = "feat: a change\n\ntai-studio-ref: my-feature-branch\n"
    assert pdr.resolve_ref(FIELD, body) == "my-feature-branch"


def test_slashed_branch_is_returned() -> None:
    body = "tai-studio-ref: area/topic\n"
    assert pdr.resolve_ref(FIELD, body) == "area/topic"


def test_full_sha_is_returned() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert pdr.resolve_ref(FIELD, f"tai-studio-ref: {sha}") == sha


def test_surrounding_whitespace_is_stripped() -> None:
    assert pdr.resolve_ref(FIELD, "tai-studio-ref:   spaced-branch   ") == "spaced-branch"


def test_first_matching_line_wins() -> None:
    body = "tai-studio-ref: first\ntai-studio-ref: second\n"
    assert pdr.resolve_ref(FIELD, body) == "first"


def test_key_with_no_value_defaults_to_main() -> None:
    assert pdr.resolve_ref(FIELD, "tai-studio-ref:   \n") == "main"


def test_only_a_line_start_key_matches() -> None:
    # A mention mid-line, or a differently-named key, is not a declaration.
    body = "see tai-studio-ref: nope\nother-ref: nope\n"
    assert pdr.resolve_ref(FIELD, body) == "main"


@pytest.mark.parametrize(
    "value",
    [
        "branch with spaces",
        "branch;rm -rf /",
        "$(whoami)",
        "-leading-dash",
        "/leading-slash",
        "with..dots",
        "back`tick`",
        "refs/heads/x",
        "refs/pull/1/head",
    ],
)
def test_malformed_value_raises(value: str) -> None:
    with pytest.raises(ValueError, match="not a branch name or 40-hex sha"):
        pdr.resolve_ref(FIELD, f"tai-studio-ref: {value}")


def test_a_different_field_name_is_read() -> None:
    body = "tai42-ref: engine-branch\n"
    assert pdr.resolve_ref("tai42-ref", body) == "engine-branch"
