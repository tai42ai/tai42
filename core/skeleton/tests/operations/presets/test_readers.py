"""Oracles for the pure body-structure readers — the byte-stable 400s a malformed
combo / schema payload raises before any store write."""

from __future__ import annotations

import pytest

from tai42_skeleton.operations import presets as preset_ops


def test_read_element_rejects_empty_string() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="non-empty string"):
        preset_ops.read_element("")


def test_read_element_rejects_non_str_non_dict() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="extension name or a"):
        preset_ops.read_element(42)


def test_read_element_rejects_missing_name() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="non-empty string 'name'"):
        preset_ops.read_element({"config": {}})


def test_read_element_rejects_missing_config() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="must carry a 'config' mapping"):
        preset_ops.read_element({"name": "x"})


def test_read_element_rejects_unexpected_keys() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="unexpected keys"):
        preset_ops.read_element({"name": "x", "config": {}, "junk": 1})


def test_read_element_accepts_name_config() -> None:
    assert preset_ops.read_element({"name": "x", "config": {"a": 1}}) == {"name": "x", "config": {"a": 1}}


def test_read_combos_rejects_empty_inner_combo() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="non-empty list"):
        preset_ops.read_combos([[]])


def test_read_create_extensions_absent_is_empty() -> None:
    assert preset_ops.read_create_extensions(False, None) == []


def test_read_create_extensions_explicit_empty_rejected() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="explicit empty"):
        preset_ops.read_create_extensions(True, [])


def test_read_create_extensions_non_list_rejected() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="must be a list of combos"):
        preset_ops.read_create_extensions(True, "nope")


def test_read_edit_extensions_absent_or_null_carries() -> None:
    assert preset_ops.read_edit_extensions(False, None) is None
    assert preset_ops.read_edit_extensions(True, None) is None


def test_read_edit_extensions_empty_clears() -> None:
    assert preset_ops.read_edit_extensions(True, []) == []


def test_read_edit_extensions_non_list_rejected() -> None:
    with pytest.raises(preset_ops.BadRequestError, match="must be a list of combos"):
        preset_ops.read_edit_extensions(True, 5)


def test_read_output_schema_variants() -> None:
    assert preset_ops.read_output_schema(None) is None
    assert preset_ops.read_output_schema({"type": "object"}) == {"type": "object"}
    with pytest.raises(preset_ops.BadRequestError, match="JSON object"):
        preset_ops.read_output_schema("nope")
