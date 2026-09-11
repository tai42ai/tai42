"""``reject_untitled_response_format``: the titles-required structured-output guard.

A JSON-Schema ``response_format`` names its forced structured output by its
top-level ``"title"``, and a ``oneOf`` container binds one structured-output name
PER variant — so each variant needs its own non-empty title, or the tool-calling
strategy mints a random ``response_format_<hex>`` name for it. ``None`` and a
pydantic class (which carries its own name) pass untouched; only ``oneOf`` fans
out for a dict schema, so an ``anyOf`` container is one spec and is not walked.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_kit.utils.render import SchemaBodyError

from tai42_agents._internal.reject import reject_untitled_response_format, resolve_response_format


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


# --------------------------------------------------------------------------- #
# resolve_response_format: the TemplatedText | dict authored-schema union       #
# --------------------------------------------------------------------------- #
_RF_RESOURCES = {
    "stored-rf": '{"title": "Stored", "type": "object", "properties": {"n": {"type": "integer"}}}',
    "not-json-rf": "this is not JSON",
}


class _RFResourceManager:
    async def render_templated_text(self, text: TemplatedText, locale=None):
        if text.id is not None:
            if text.id not in _RF_RESOURCES:
                # The real manager raises TemplateNotFoundError; this package does not import the
                # skeleton, so a plain lookup failure stands in — the resolver catches it loudly.
                raise LookupError(f"no stored resource {text.id!r}")
            return _RF_RESOURCES[text.id]
        assert text.content is not None
        return text.content


class _RFStorage:
    resource_manager = _RFResourceManager()


class _RFApp:
    storage = _RFStorage()


def test_resolve_response_format_passthrough() -> None:
    # None, a pydantic class and an inline titled dict pass through unchanged (no render).
    assert asyncio.run(resolve_response_format("agent", None)) is None
    assert asyncio.run(resolve_response_format("agent", _Model)) is _Model
    inline = {"title": "T", "type": "object"}
    assert asyncio.run(resolve_response_format("agent", inline)) == inline


def test_resolve_response_format_by_id_renders_and_parses() -> None:
    async def _run():
        with tai42_app.bound(_RFApp()):
            return await resolve_response_format("agent", TemplatedText(id="stored-rf"))

    assert asyncio.run(_run()) == {"title": "Stored", "type": "object", "properties": {"n": {"type": "integer"}}}


def test_resolve_response_format_by_id_unfetchable_fails_loudly() -> None:
    async def _run():
        with tai42_app.bound(_RFApp()):
            await resolve_response_format("agent", TemplatedText(id="missing-rf"))

    with pytest.raises(SchemaBodyError, match="could not be rendered"):
        asyncio.run(_run())


def test_resolve_response_format_by_id_invalid_json_fails_loudly() -> None:
    async def _run():
        with tai42_app.bound(_RFApp()):
            await resolve_response_format("agent", TemplatedText(id="not-json-rf"))

    with pytest.raises(SchemaBodyError, match="did not render to valid JSON"):
        asyncio.run(_run())
