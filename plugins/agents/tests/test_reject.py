"""``reject_untitled_response_format``: the titles-required structured-output guard.

A JSON-Schema ``response_format`` names its forced structured output by its
top-level ``"title"``, and a ``oneOf`` container binds one structured-output name
PER variant — so each variant needs its own non-empty title, or the tool-calling
strategy mints a random ``response_format_<hex>`` name for it. ``None`` and a
pydantic class (which carries its own name) pass untouched; only ``oneOf`` fans
out for a dict schema, so an ``anyOf`` container is one spec and is not walked.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tai42_agents._internal.reject import reject_untitled_response_format


class _Model(BaseModel):
    value: int


def test_none_and_pydantic_class_pass() -> None:
    reject_untitled_response_format("agent", None)
    reject_untitled_response_format("agent", _Model)


def test_plain_titled_dict_passes() -> None:
    reject_untitled_response_format("agent", {"title": "T", "type": "object"})


def test_missing_top_level_title_raises() -> None:
    with pytest.raises(ValueError, match="top-level 'title'"):
        reject_untitled_response_format("agent", {"type": "object"})


def test_titled_oneof_with_titled_variants_passes() -> None:
    schema = {"title": "T", "oneOf": [{"title": "A", "type": "object"}, {"title": "B", "type": "object"}]}
    reject_untitled_response_format("agent", schema)


def test_titled_oneof_with_untitled_variant_raises() -> None:
    # Each oneOf variant binds its own tool name, so a top-level title cannot
    # stand in for a missing variant title.
    schema = {"title": "T", "oneOf": [{"title": "A", "type": "object"}, {"type": "object"}]}
    with pytest.raises(ValueError, match="oneOf variants must each"):
        reject_untitled_response_format("agent", schema)


def test_titled_oneof_with_blank_title_variant_raises() -> None:
    schema = {"title": "T", "oneOf": [{"title": "A", "type": "object"}, {"title": "   ", "type": "object"}]}
    with pytest.raises(ValueError, match="oneOf variants must each"):
        reject_untitled_response_format("agent", schema)


def test_nested_oneof_untitled_leaf_raises() -> None:
    # A nested ``oneOf`` is fanned out the same way, so an untitled deep leaf is
    # still named.
    schema = {
        "title": "T",
        "oneOf": [{"title": "A", "type": "object"}, {"oneOf": [{"type": "object"}]}],
    }
    with pytest.raises(ValueError, match="oneOf variants must each"):
        reject_untitled_response_format("agent", schema)


def test_untitled_anyof_variant_passes() -> None:
    # Only ``oneOf`` fans out into per-variant names; an ``anyOf`` container is a
    # single spec named by the top-level title, so its variants are not walked.
    schema = {"title": "T", "anyOf": [{"type": "object"}, {"type": "object"}]}
    reject_untitled_response_format("agent", schema)


def test_error_names_the_agent() -> None:
    schema = {"title": "T", "oneOf": [{"type": "object"}]}
    with pytest.raises(ValueError, match="my_agent response_format"):
        reject_untitled_response_format("my_agent", schema)
