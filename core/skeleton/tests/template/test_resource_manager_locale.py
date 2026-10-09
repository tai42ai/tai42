"""The rendering layer resolves the subject's locale: a stored template resolves to its
per-locale variant down a declared fallback chain (refusing loudly when none exists), and
the ``list_format`` filter formats a sequence with that locale's CLDR patterns."""

from __future__ import annotations

import pytest
from tai42_contract.storage import Storage
from tai42_contract.template import TemplatedText
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError


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

    return ResourceManager(registry.provider, on_evicted=lambda _eviction: None)


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


# -- absent ids are remembered until an eviction seam drops them ---------------------------


class _CountingStorage(_InMemoryStorage):
    """In-memory storage counting every read, optionally failing reads of one id."""

    def __init__(self, items: dict[str, str] | None = None) -> None:
        super().__init__(items)
        self.reads: list[str] = []
        self.broken: set[str] = set()

    async def load(self, path: str) -> str:
        self.reads.append(path)
        if path in self.broken:
            raise ConnectionError(f"storage unreachable for {path}")
        return await super().load(path)


def _counting_manager(items: dict[str, str]) -> tuple[ResourceManager, _CountingStorage]:
    storage = _CountingStorage(items)
    registry = StorageRegistry()

    @registry.register_storage
    class Provider(_CountingStorage):
        def __new__(cls) -> _CountingStorage:  # type: ignore[misc]
            return storage

    return ResourceManager(registry.provider, on_evicted=lambda _eviction: None), storage


async def test_a_locale_render_reads_the_absent_variants_once_then_never() -> None:
    manager, storage = _counting_manager({"welcome": "default"})

    assert await manager.render_by_id("welcome", locale="he-IL") == "default"
    assert storage.reads == ["welcome@he-IL", "welcome@he", "welcome"]
    storage.reads.clear()

    for _ in range(3):
        assert await manager.render_by_id("welcome", locale="he-IL") == "default"
    assert storage.reads == []


async def test_uploading_an_absent_variant_makes_the_next_render_use_it() -> None:
    manager, _storage = _counting_manager({"welcome": "default"})
    assert await manager.render_by_id("welcome", locale="he") == "default"

    await manager.upload_template("welcome@he", "shalom")

    assert await manager.render_by_id("welcome", locale="he") == "shalom"


async def test_a_bus_eviction_of_an_absent_variant_makes_the_next_render_use_it() -> None:
    manager, storage = _counting_manager({"welcome": "default"})
    assert await manager.render_by_id("welcome", locale="he") == "default"

    # Another worker stored the variant and broadcast the eviction this worker applies.
    storage.items["welcome@he"] = "shalom"
    manager.evict_compiled("welcome@he")

    assert await manager.render_by_id("welcome", locale="he") == "shalom"


async def test_a_directory_eviction_and_a_cache_clear_forget_absent_ids() -> None:
    manager, storage = _counting_manager({"dir/welcome": "default", "other": "x"})
    assert await manager.render_by_id("dir/welcome", locale="he") == "default"
    assert await manager.render_by_id("other", locale="he") == "x"

    storage.items["dir/welcome@he"] = "shalom"
    manager.evict_dir("dir")
    assert await manager.render_by_id("dir/welcome", locale="he") == "shalom"

    storage.items["other@he"] = "y"
    assert await manager.render_by_id("other", locale="he") == "x"  # still remembered absent
    manager.clear_cache()
    assert await manager.render_by_id("other", locale="he") == "y"


async def test_an_absent_bare_id_raises_each_time_without_a_read() -> None:
    manager, storage = _counting_manager({})

    for _ in range(2):
        with pytest.raises(TemplateNotFoundError, match="missing"):
            await manager.render_by_id("missing")
    assert storage.reads == ["missing"]


async def test_an_absent_include_target_is_read_once() -> None:
    manager, storage = _counting_manager({"page": "a{% include 'part' ignore missing %}b"})

    assert await manager.render_by_id("page") == "ab"
    manager.evict_compiled("page")  # recompile the page; the include target stays absent
    storage.reads.clear()
    assert await manager.render_by_id("page") == "ab"

    assert storage.reads == ["page"]


async def test_a_storage_error_is_raised_each_time_and_never_recorded() -> None:
    manager, storage = _counting_manager({"welcome": "default"})
    storage.broken.add("welcome@he")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await manager.render_by_id("welcome", locale="he")
    assert storage.reads.count("welcome@he") == 2

    storage.broken.clear()
    assert await manager.render_by_id("welcome", locale="he") == "default"


async def test_the_absent_cache_honours_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.template import absent_ids as absent_ids_module

    clock = {"now": 1000.0}
    monkeypatch.setenv("TEMPLATE_CACHE_TTL", "30")
    reset_all_settings()
    monkeypatch.setattr(absent_ids_module.time, "monotonic", lambda: clock["now"])
    manager, storage = _counting_manager({"welcome": "default"})
    assert await manager.render_by_id("welcome", locale="he") == "default"

    storage.items["welcome@he"] = "shalom"  # created directly in the backend, outside the platform
    clock["now"] += 29
    assert await manager.render_by_id("welcome", locale="he") == "default"
    clock["now"] += 2
    manager.evict_compiled("welcome")  # the compiled default has its own TTL; drop it here
    assert await manager.render_by_id("welcome", locale="he") == "shalom"
    reset_all_settings()


async def test_the_absent_cache_is_bounded_by_the_max_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPLATE_CACHE_MAX_SIZE", "2")
    reset_all_settings()
    manager, storage = _counting_manager({})

    for name in ("a", "b", "c"):  # "a" is the oldest and is pushed out by "c"
        with pytest.raises(TemplateNotFoundError):
            await manager.render_by_id(name)
    storage.reads.clear()
    for name in ("b", "c", "a"):
        with pytest.raises(TemplateNotFoundError):
            await manager.render_by_id(name)

    assert storage.reads == ["a"]
    reset_all_settings()


async def test_with_caching_off_every_render_reads_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPLATE_CACHE_TTL", "0")
    reset_all_settings()
    manager, storage = _counting_manager({"welcome": "default"})

    await manager.render_by_id("welcome", locale="he")
    await manager.render_by_id("welcome", locale="he")

    assert storage.reads.count("welcome@he") == 2
    reset_all_settings()
