"""Tests for the ``RoleDefinition`` contract type — the operator-authored role
shape shared by the enforcer/intersection-pass, the management operations, and the
generated Studio SDK.

A role composes a KEPT jq security base (Layer 1, carried on ``condition``) with an
editable per-tag ACCESS LEVEL map ``grants`` (Layer 2: feature-group TAG name →
``none``/``read``/``write``). These tests pin the validated shape, its defaults, its
round-trip, and its fail-loud validation — NOT enforcement (skeleton), a store, or
any raw-jq authoring surface.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.access_control import RoleDefinition


def test_exported_from_package():
    # The public import the skeleton and Studio SDK consume.
    from tai42_contract.access_control import RoleDefinition as Exported

    assert Exported is RoleDefinition


def test_minimal_defaults():
    role = RoleDefinition(name="editor", description="Edits things", grants={"presets": "write"})
    # scopes default to ["*"]; the jq base and base tier are unset; not allow-all.
    assert role.scopes == ["*"]
    assert role.condition is None
    assert role.condition_id is None
    assert role.condition_kwargs is None
    assert role.base_tier is None
    assert role.allow_all is False
    assert role.grants == {"presets": "write"}


def test_default_scopes_are_not_shared_between_instances():
    # A mutable default must not be shared state across instances.
    first = RoleDefinition(name="a", description="", grants={})
    second = RoleDefinition(name="b", description="", grants={})
    first.scopes.append("extra")
    assert second.scopes == ["*"]


def test_round_trips_through_model_dump():
    role = RoleDefinition(
        name="reviewer",
        description="Read-only reviewer",
        scopes=["read:things"],
        condition='.request.method == "GET"',
        base_tier="viewer",
        allow_all=False,
        grants={"presets": "read", "hooks": "read", "access-control": "none"},
    )
    dumped = role.model_dump()
    rebuilt = RoleDefinition(**dumped)
    assert rebuilt == role
    # The jq base and every Layer-2 field survive the round-trip.
    assert rebuilt.condition == '.request.method == "GET"'
    assert rebuilt.base_tier == "viewer"
    assert rebuilt.allow_all is False
    assert rebuilt.grants == {"presets": "read", "hooks": "read", "access-control": "none"}


def test_all_three_levels_are_valid():
    role = RoleDefinition(
        name="mixed",
        description="",
        grants={"presets": "none", "hooks": "read", "users": "write"},
    )
    assert role.grants == {"presets": "none", "hooks": "read", "users": "write"}


def test_tag_mapped_to_none_is_a_valid_stored_value():
    # `none` is an explicit no-access — equivalent to absence at enforce time,
    # but a valid stored level the UI may keep.
    role = RoleDefinition(name="editor", description="", grants={"presets": "none"})
    assert role.grants == {"presets": "none"}


def test_absent_tag_is_not_stored():
    # An absent tag means level `none` (deny) to the enforcer; the map only
    # carries the tags the role authored a level for.
    role = RoleDefinition(name="editor", description="", grants={"presets": "read"})
    assert "hooks" not in role.grants


def test_allow_all_admin_tier_with_empty_grants_is_valid():
    role = RoleDefinition(name="admin", description="All access", allow_all=True, grants={})
    assert role.allow_all is True
    assert role.grants == {}
    assert role.condition is None


def test_allow_all_with_non_empty_grants_raises():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        RoleDefinition(name="admin", description="", allow_all=True, grants={"presets": "read"})


def test_empty_grants_without_allow_all_is_valid_and_not_allow_all():
    # A fully-locked-among-grantable-surfaces role — denies among the grantable
    # surfaces in the skeleton, NOT allow-all.
    role = RoleDefinition(name="locked", description="", grants={})
    assert role.grants == {}
    assert role.allow_all is False


def test_invalid_level_raises():
    # A level outside none/read/write is rejected by the Literal type at
    # construction — never coerced.
    with pytest.raises(ValidationError):
        RoleDefinition(name="editor", description="", grants={"presets": "admin"})  # pyright: ignore[reportArgumentType]


def test_empty_string_tag_raises():
    with pytest.raises(ValidationError, match="non-empty feature-group"):
        RoleDefinition(name="editor", description="", grants={"": "read"})


def test_whitespace_in_tag_raises():
    with pytest.raises(ValidationError, match="whitespace"):
        RoleDefinition(name="editor", description="", grants={"pre sets": "read"})


def test_whitespace_only_tag_raises():
    with pytest.raises(ValidationError, match="whitespace"):
        RoleDefinition(name="editor", description="", grants={"   ": "read"})


def test_empty_name_raises():
    with pytest.raises(ValidationError, match="non-empty stable identifier"):
        RoleDefinition(name="", description="", grants={})


def test_name_with_whitespace_raises():
    with pytest.raises(ValidationError, match="whitespace"):
        RoleDefinition(name="my role", description="", grants={})


@pytest.mark.parametrize("bad_name", ["a/b", "a\\b"])
def test_name_with_path_separator_raises(bad_name: str):
    with pytest.raises(ValidationError, match="path separators"):
        RoleDefinition(name=bad_name, description="", grants={})
