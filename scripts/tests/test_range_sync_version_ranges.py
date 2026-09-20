"""Tests for scripts/range_sync.py: version-range derivation, requirement parsing and cross-major detection.

The shared sample-tree builders live in _range_sync_support."""

from __future__ import annotations

import pytest

import range_sync


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.3.0", ">=0.3,<0.4"),
        ("0.5.1", ">=0.5,<0.6"),
        ("0.2.2", ">=0.2,<0.3"),
        ("0.4.0", ">=0.4,<0.5"),
        # floor is ALWAYS minor precision (>=major.minor); only the cap flips at
        # the 1.0 breaking boundary (next minor pre-1.0, next major from 1.0).
        ("1.2.3", ">=1.2,<2"),
        ("1.0.0", ">=1.0,<2"),
        ("2.0.0", ">=2.0,<3"),
    ],
)
def test_derive_range(version: str, expected: str):
    assert range_sync.derive_range(version) == expected


def test_parse_requirement_preserves_extras_and_marker():
    parsed = range_sync.parse_requirement("tai42-kit[llm,jq,redis]>=0.2,<0.4; python_version >= '3.13'")
    assert parsed is not None
    assert parsed.name == "tai42-kit"
    assert parsed.extras == "[llm,jq,redis]"
    assert parsed.specifier == ">=0.2,<0.4"
    assert parsed.marker == "; python_version >= '3.13'"
    assert parsed.with_specifier(">=0.3,<0.4") == ("tai42-kit[llm,jq,redis]>=0.3,<0.4; python_version >= '3.13'")


def test_parse_requirement_versionless():
    parsed = range_sync.parse_requirement("tai42-kit[curl]")
    assert parsed is not None
    assert parsed.name == "tai42-kit"
    assert parsed.extras == "[curl]"
    assert parsed.specifier == ""
    assert parsed.marker == ""


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (">=1.2,<2", ">=2.0,<3", True),  # 1.x -> 2.x: floor major rises
        (">=1.2,<2", ">=1.5,<2", False),  # minor bump within major 1
        (">=2.0,<3", ">=2.1,<3", False),  # minor bump within major 2
        (">=0.3,<0.4", ">=0.5,<0.6", False),  # pre-1.0: major stays 0, never cross-major
        ("", ">=2.0,<3", False),  # no old floor -> no comparable major
        (">=1.2,<2", "", False),  # no new floor -> no comparable major
        (">=1.2,<3", ">=1.5,<2", True),  # widened cap narrowed 3 -> 2 within floor major 1
        ("~=1.2", ">=2.0,<3", True),  # compat spec implies (1, 1); floor major rises
        ("==1.2.3", ">=1.5,<2", True),  # exact spec implies (1, 1); cap major differs
        ("~=1.2", ">=1.5,<2", True),  # compat (1, 1) vs (1, 2): cap major differs
    ],
)
def test_is_cross_major(old: str, new: str, expected: bool):
    assert range_sync.is_cross_major(old, new) is expected
