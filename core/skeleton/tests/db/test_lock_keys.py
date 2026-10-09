"""The skeleton's advisory-lock keys are declared once and never collide."""

from __future__ import annotations

from tai42_kit.db import MIGRATION_LOCK_KEY

from tai42_skeleton.db.lock_keys import FIRST_PRINCIPAL_LOCK_KEY, MARKETPLACE_LOCK_KEY, PRESET_LOCK_NAMESPACE

_INT64_MAX = 2**63 - 1
_INT32_MAX = 2**31 - 1


def test_the_single_key_locks_are_distinct_positive_bigints() -> None:
    keys = [MARKETPLACE_LOCK_KEY, FIRST_PRINCIPAL_LOCK_KEY, MIGRATION_LOCK_KEY]
    assert len(set(keys)) == len(keys)
    assert all(0 < key <= _INT64_MAX for key in keys)


def test_the_preset_namespace_is_a_positive_int32() -> None:
    assert 0 < PRESET_LOCK_NAMESPACE <= _INT32_MAX
