"""The rendering layer resolves the subject's locale: a stored template resolves to its
per-locale variant down a declared fallback chain (refusing loudly when none exists), and
the ``list_format`` filter formats a sequence with that locale's CLDR patterns."""

from __future__ import annotations

import pytest
from tai42_contract.storage import Storage
from tai42_contract.template import TemplatedText

from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError


class _InMemoryStorage(Storage):
    def __init__(self, items: dict[str, str] | None = None) -> None:
        self.items: dict[str, str] = dict(items or {})

    async def load(self, path: str) -> str:
        try:
            return self.items[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def list(self) -> list[str]:
        return sorted(self.items)

    async def upload(self, path: str, content: str) -> None:
        self.items[path] = content

    async def delete(self, path: str) -> None:
        self.items.pop(path, None)

    async def delete_dir(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self.items if k.startswith(prefix)]:
            del self.items[key]


def _manager(items: dict[str, str]) -> ResourceManager:
    registry = StorageRegistry()

    @registry.register_storage
    class Provider(_InMemoryStorage):
        def __init__(self) -> None:
            super().__init__(items)

    return ResourceManager(registry.provider)


async def test_locale_variant_chain_prefers_specific_then_language_then_default() -> None:
    manager = _manager(
        {
            "welcome": "default",
            "welcome@he": "shalom",
            "welcome@he-IL": "shalom israel",
        }
    )
    assert await manager.render_by_id("welcome", locale="he-IL") == "shalom israel"
    assert await manager.render_by_id("welcome", locale="he") == "shalom"
    # No he-IL variant present -> falls back to the language variant.
    manager2 = _manager({"welcome": "default", "welcome@he": "shalom"})
    assert await manager2.render_by_id("welcome", locale="he-IL") == "shalom"
    # No locale variant at all -> the bare default variant is the last chain link.
    assert await manager2.render_by_id("welcome", locale="en-US") == "default"


async def test_no_variant_and_no_default_refuses_loudly() -> None:
    manager = _manager({"welcome@fr": "bonjour"})
    with pytest.raises(TemplateLocaleNotFoundError, match=r"welcome.*he"):
        await manager.render_by_id("welcome", locale="he")


async def test_none_locale_renders_bare_template_unchanged() -> None:
    manager = _manager({"welcome": "hi {{ name }}", "welcome@he": "shalom"})
    assert await manager.render_by_id("welcome", {"name": "Ada"}, locale=None) == "hi Ada"


async def test_list_format_uses_the_render_locale() -> None:
    manager = _manager({})
    text = TemplatedText(content="{{ x | list_format }}", kwargs={"x": ["a", "b", "c"]})
    he = await manager.render_templated_text(text, "he")
    en = await manager.render_templated_text(text, "en")
    assert he != en
    assert "and" in en


async def test_list_format_without_a_locale_raises() -> None:
    manager = _manager({})
    with pytest.raises(ValueError, match="needs the subject's locale"):
        await manager.render_templated_text(
            TemplatedText(content="{{ x | list_format }}", kwargs={"x": ["a", "b"]}), None
        )


async def test_list_format_unknown_locale_raises() -> None:
    manager = _manager({})
    with pytest.raises(ValueError, match="no CLDR list patterns"):
        await manager.render_templated_text(
            TemplatedText(content="{{ x | list_format }}", kwargs={"x": ["a", "b"]}), "zz"
        )


async def test_reserved_locale_context_key_collision_raises() -> None:
    manager = _manager({})
    with pytest.raises(ValueError, match="reserved render variable"):
        await manager.render_templated_text(
            TemplatedText(content="{{ ok }}", kwargs={"_tai_locale": "he", "ok": "x"}), "he"
        )
