"""Template input-schema reads: ``get_template_schema`` (typed inference, degrading to a
names-only schema for templates ``jinja2schema`` cannot infer) and ``find_undeclared_variables``,
from inline content or a stored id. All storage is an in-memory ``Storage`` double."""

from __future__ import annotations

import pytest
from jinja2 import TemplateError
from tai42_contract.template import TemplatedText

from tai42_skeleton.template import TemplateNotFoundError

from ._manager_fixtures import _manager

# --- schema inference -------------------------------------------------------


async def test_get_template_schema_from_content() -> None:
    manager, _ = _manager()
    schema = await manager.get_template_schema(content="{{ a }}")
    assert schema["type"] == "object"
    assert "a" in schema["properties"]


async def test_get_template_schema_from_template_id() -> None:
    manager, _ = _manager({"s.j2": "{{ a }}"})
    schema = await manager.get_template_schema(template_id="s.j2")
    assert "a" in schema["properties"]


async def test_get_template_schema_rejects_both() -> None:
    manager, _ = _manager()
    with pytest.raises(ValueError, match="not both"):
        await manager.get_template_schema(content="x", template_id="y")


async def test_get_template_schema_rejects_neither() -> None:
    manager, _ = _manager()
    with pytest.raises(ValueError, match="either a template or a template_id"):
        await manager.get_template_schema()


async def test_get_template_schema_empty_template_is_empty_schema() -> None:
    """A present-but-empty template infers an empty (variable-less) schema rather
    than raising not-found."""
    manager, _ = _manager({"s.j2": ""})
    schema = await manager.get_template_schema(template_id="s.j2")
    assert schema.get("properties", {}) == {}


async def test_get_template_schema_missing_template_id_raises() -> None:
    manager, _ = _manager({"present.j2": "{{ a }}"})
    with pytest.raises(TemplateNotFoundError, match="not found"):
        await manager.get_template_schema(template_id="absent.j2")


# --- schema inference: degraded fallback ------------------------------------
#
# ``jinja2schema`` covers only a subset of Jinja and rejects these VALID,
# RENDERABLE templates with errors that are not ``jinja2.TemplateError``
# (InferException subclasses, a bare ``Exception`` from a missing stmt visitor,
# a ``ValueError`` out of its own loader). A read of a renderable template must
# never fail on that, so each degrades to a names-only schema.

_INFERENCE_UNSUPPORTED = [
    pytest.param("{{ {'a': 1} }}", [], id="dict-literal"),
    pytest.param("{{ cfg | default({'a':1}) }}", ["cfg"], id="dict-default-filter"),
    pytest.param("{% set x %}hi{% endset %}", [], id="set-block"),
    pytest.param("{% with a = 1 %}{{a}}{% endwith %}", [], id="with-block"),
    pytest.param("{% include 'partial.j2' %}", [], id="include"),
    pytest.param("{{ payload | tojson }}", ["payload"], id="unknown-filter"),
    pytest.param("{% include 'partial.j2' %}{{ b }}{{ a }}", ["a", "b"], id="include-plus-vars"),
]


@pytest.mark.parametrize(("content", "expected_vars"), _INFERENCE_UNSUPPORTED)
async def test_get_template_schema_degrades_for_uninferable_templates(content: str, expected_vars: list[str]) -> None:
    """Inference failure yields the variables the app's own environment sees, marked
    ``partial`` — never an error."""
    manager, _ = _manager()
    schema = await manager.get_template_schema(content=content)
    assert schema["x-tai42-inference"] == "partial"
    assert schema["type"] == "object"
    assert sorted(schema["properties"]) == expected_vars
    assert all(schema["properties"][name] == {"title": name} for name in expected_vars)
    # Nothing is known-required in a degraded schema.
    assert schema["required"] == []


@pytest.mark.parametrize(("content", "expected_vars"), _INFERENCE_UNSUPPORTED)
async def test_uninferable_templates_still_render(content: str, expected_vars: list[str]) -> None:
    """The premise of the degrade: every template above is a working template, so a
    500 on reading its schema contradicted a 200 on rendering it."""
    manager, _ = _manager({"partial.j2": "P"})
    rendered = await manager.render_templated_text(
        TemplatedText(content=content, kwargs={"cfg": {"a": 1}, "payload": {"k": 1}, "a": "A", "b": "B"})
    )
    assert isinstance(rendered, str)


@pytest.mark.parametrize("content", ["{{ a }}", "{% set x = 1 %}"])
async def test_get_template_schema_keeps_rich_inference_where_supported(content: str) -> None:
    """The enricher still runs: a template jinja2schema understands keeps its typed,
    required-aware schema and carries no degrade marker."""
    manager, _ = _manager()
    schema = await manager.get_template_schema(content=content)
    assert "x-tai42-inference" not in schema


async def test_get_template_schema_rich_inference_carries_types() -> None:
    manager, _ = _manager()
    schema = await manager.get_template_schema(content="{{ a }}")
    assert schema["required"] == ["a"]
    assert "anyOf" in schema["properties"]["a"]


async def test_get_template_schema_broken_jinja_propagates() -> None:
    """Author-broken Jinja is not degraded away: the app's own parse raises, so the
    HTTP surface can map it to an honest 400."""
    manager, _ = _manager()
    with pytest.raises(TemplateError):
        await manager.get_template_schema(content="{{ unclosed")


async def test_get_template_schema_degraded_from_stored_template() -> None:
    """The stored-id path degrades exactly like the inline one."""
    manager, _ = _manager({"d.j2": "{{ {'a': 1} }}{{ who }}"})
    schema = await manager.get_template_schema(template_id="d.j2")
    assert schema["x-tai42-inference"] == "partial"
    assert sorted(schema["properties"]) == ["who"]


# --- undeclared variables ---------------------------------------------------


async def test_find_undeclared_variables_from_content() -> None:
    manager, _ = _manager()
    assert await manager.find_undeclared_variables(content="{{ a }}{{ b }}") == {"a", "b"}


async def test_find_undeclared_variables_from_template_id() -> None:
    manager, _ = _manager({"v.j2": "{{ a }}"})
    assert await manager.find_undeclared_variables(template_id="v.j2") == {"a"}


async def test_find_undeclared_variables_rejects_both() -> None:
    manager, _ = _manager()
    with pytest.raises(ValueError, match="not both"):
        await manager.find_undeclared_variables(content="x", template_id="y")


async def test_find_undeclared_variables_rejects_neither() -> None:
    manager, _ = _manager()
    with pytest.raises(ValueError, match="either a template or a template_id"):
        await manager.find_undeclared_variables()


async def test_find_undeclared_variables_empty_template_is_empty_set() -> None:
    """A present-but-empty template has no undeclared variables rather than
    raising not-found."""
    manager, _ = _manager({"v.j2": ""})
    assert await manager.find_undeclared_variables(template_id="v.j2") == set()


async def test_find_undeclared_variables_missing_template_id_raises() -> None:
    manager, _ = _manager({"present.j2": "{{ a }}"})
    with pytest.raises(TemplateNotFoundError, match="not found"):
        await manager.find_undeclared_variables(template_id="absent.j2")
