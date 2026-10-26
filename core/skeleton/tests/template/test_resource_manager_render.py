"""ResourceManager rendering: the sandboxed engine, ``render_templated_text`` from inline content
and from a stored id, and the fetch + compile path of a stored template. All storage is an
in-memory ``Storage`` double."""

from __future__ import annotations

import pytest
from jinja2.exceptions import SecurityError
from tai42_contract.template import TemplatedText

from tai42_skeleton.template import ResourceManager, TemplateNotFoundError
from tai42_skeleton.template.render_locale import with_locale

from ._manager_fixtures import _manager

# --- sandboxed engine (SSTI is blocked) -------------------------------------


async def test_sandbox_blocks_dunder_traversal_chain() -> None:
    """The shared engine is a ``SandboxedEnvironment``: a full dunder traversal +
    call chain (the classic SSTI-to-RCE reach for subprocess primitives) raises
    ``SecurityError`` at render, not host code execution."""
    manager, _ = _manager()
    with pytest.raises(SecurityError):
        await manager.render_templated_text(TemplatedText(content="{{ ''.__class__.__mro__[1].__subclasses__() }}"))


async def test_sandbox_leaves_normal_rendering_intact() -> None:
    """Ordinary variable/filter rendering is unaffected by the sandbox."""
    manager, _ = _manager()
    rendered = await manager.render_templated_text(TemplatedText(content="{{ name | upper }}", kwargs={"name": "ada"}))
    assert rendered == "ADA"


# --- render_templated_text: the two sources ---------------------------------


async def test_render_templated_text_empty_content_renders_blank() -> None:
    """An explicit empty inline template renders as '' (content is honoured, not
    skipped as falsy)."""
    manager, _ = _manager()
    assert await manager.render_templated_text(TemplatedText(content="")) == ""


async def test_render_templated_text_via_stored_id() -> None:
    manager, _ = _manager({"g.j2": "Hi {{ name }}"})
    rendered = await manager.render_templated_text(TemplatedText(id="g.j2", kwargs={"name": "Z"}))
    assert rendered == "Hi Z"


_REFERENCE_TEXTS = [
    TemplatedText(content=""),
    TemplatedText(content="Hi {{ who }}", kwargs={"who": "there"}),
    TemplatedText(content="{{ x | upper }}", kwargs={"x": "ada"}),
    TemplatedText(id="g.j2", kwargs={"name": "Z"}),
    TemplatedText(id="empty.j2"),
]


async def _reference_render(manager: ResourceManager, text: TemplatedText, locale: str | None) -> str:
    """The rendering path spelled out: a stored id resolves through the compiled-template
    path, inline content compiles in the sandboxed engine, and both render with the
    text's own kwargs under the resolved locale."""
    if text.id is not None:
        return await manager.render_by_id(text.id, text.kwargs, locale=locale)
    assert text.content is not None
    context = with_locale(text.kwargs, locale)
    with manager._render_scope():
        return manager._render_cache.inline(text.content).render(**context)


@pytest.mark.parametrize("text", _REFERENCE_TEXTS)
@pytest.mark.parametrize("locale", [None, "he"])
async def test_render_templated_text_matches_the_reference_render(text: TemplatedText, locale: str | None) -> None:
    manager, _ = _manager({"g.j2": "Hi {{ name }}", "empty.j2": ""})
    assert await manager.render_templated_text(text, locale) == await _reference_render(manager, text, locale)


# --- fetch + compile error path ---------------------------------------------


async def test_fetch_template_returns_content() -> None:
    manager, _ = _manager({"t.j2": "body"})
    assert await manager.fetch_template("t.j2") == "body"


async def test_fetch_and_compile_empty_content_renders_empty() -> None:
    """A present-but-empty stored template is valid: it renders to empty, never
    mistaken for a missing template."""
    manager, _ = _manager({"empty.j2": ""})
    assert await manager.render_by_id("empty.j2") == ""


async def test_fetch_and_compile_missing_template_raises() -> None:
    """A genuinely missing template (storage raises FileNotFoundError) surfaces
    as a typed ``TemplateNotFoundError`` (mapped to 404 by the HTTP surface)."""
    manager, _ = _manager({"present.j2": "x"})
    with pytest.raises(TemplateNotFoundError, match="not found"):
        await manager.render_by_id("absent.j2")
