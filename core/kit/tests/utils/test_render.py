"""The kit-side renderer: it hands a templated text to the bound app's resource manager.

The helper adds no rendering of its own — it is the seam that lets code below the
skeleton render a templated text — so these tests pin what it passes through and what it
returns.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText

from tai42_kit.utils.render import SchemaBodyError, render_templated_text, resolve_schema_body


class _FakeResourceManager:
    """Records every text it is handed and answers a stored id from a map."""

    def __init__(self) -> None:
        self.calls: list[tuple[TemplatedText, str | None]] = []
        self.resources: dict[str, str] = {}

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        self.calls.append((text, locale))
        if text.id is not None:
            return self.resources[text.id]
        return f"{text.content}|{sorted(text.kwargs.items())}"


@pytest.fixture
def manager():
    fake = _FakeResourceManager()
    app: Any = SimpleNamespace(storage=SimpleNamespace(resource_manager=fake))
    with tai42_app.bound(app):
        yield fake


async def test_inline_text_renders_through_the_manager(manager: _FakeResourceManager) -> None:
    text = TemplatedText(content="Hi {{ who }}", kwargs={"who": "Ada"})
    assert await render_templated_text(text) == "Hi {{ who }}|[('who', 'Ada')]"
    assert manager.calls == [(text, None)]


async def test_stored_text_renders_through_the_manager(manager: _FakeResourceManager) -> None:
    manager.resources["greeting"] = "Shalom"
    assert await render_templated_text(TemplatedText(id="greeting")) == "Shalom"


async def test_locale_reaches_the_manager(manager: _FakeResourceManager) -> None:
    manager.resources["greeting"] = "Shalom"
    await render_templated_text(TemplatedText(id="greeting"), "he-IL")
    assert manager.calls[0][1] == "he-IL"


async def test_a_manager_failure_propagates(manager: _FakeResourceManager) -> None:
    with pytest.raises(KeyError):
        await render_templated_text(TemplatedText(id="absent"))


# --------------------------------------------------------------------------- #
# resolve_schema_body: the TemplatedText | dict authored-schema-body union      #
# --------------------------------------------------------------------------- #
async def test_resolve_schema_body_none_and_inline_dict_passthrough(manager: _FakeResourceManager) -> None:
    # ``None`` is the unset body; an inline dict is the schema document itself, returned as-is
    # with no render (the inline author sees no change).
    assert await resolve_schema_body("f", None) is None
    inline = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert await resolve_schema_body("f", inline) is inline
    assert manager.calls == []


async def test_resolve_schema_body_by_id_renders_and_parses(manager: _FakeResourceManager) -> None:
    manager.resources["s"] = '{"type": "object", "properties": {"n": {"type": "integer"}}}'
    assert await resolve_schema_body("f", TemplatedText(id="s")) == {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
    }


async def test_resolve_schema_body_by_id_unfetchable_raises(manager: _FakeResourceManager) -> None:
    with pytest.raises(SchemaBodyError, match=r"stored id 'absent'.*could not be rendered"):
        await resolve_schema_body("myfield", TemplatedText(id="absent"))


async def test_resolve_schema_body_invalid_json_raises(manager: _FakeResourceManager) -> None:
    manager.resources["s"] = "not json at all"
    with pytest.raises(SchemaBodyError, match="did not render to valid JSON"):
        await resolve_schema_body("myfield", TemplatedText(id="s"))


async def test_resolve_schema_body_non_object_raises(manager: _FakeResourceManager) -> None:
    manager.resources["s"] = "[1, 2, 3]"
    with pytest.raises(SchemaBodyError, match="rendered to a list, not a JSON object"):
        await resolve_schema_body("myfield", TemplatedText(id="s"))
