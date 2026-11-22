"""The render seam's error contract: a text that cannot be rendered as authored raises a
``TemplateRenderError``; every other failure leaves the manager as itself.

The manager is the one place that tells the two apart: what the author's text raises while it
is fetched, compiled and rendered becomes a member of the contract family; what storage, the
network or the manager's own wiring raises (a fault of the loader, the unconfigured provider)
propagates unchanged, even when it surfaces from inside an ``{% include %}``.
"""

from __future__ import annotations

import re

import pytest
from tai42_contract.template import (
    TemplateBodyError,
    TemplatedText,
    TemplateNotFoundError,
    TemplateNotTextError,
    TemplateRenderError,
)

from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template.render_locale import LOCALE_CONTEXT_KEY

from ._manager_fixtures import _InMemoryStorage, _manager, _no_holders


class _FaultyStorage(_InMemoryStorage):
    """Answers from the map, raises ``fault`` for ``faulty`` and a decode error for ``binary``."""

    def __init__(self, items: dict[str, str], *, faulty: str = "", fault: Exception | None = None) -> None:
        super().__init__(items)
        self.faulty = faulty
        self.fault = fault

    async def load(self, path: str) -> str:
        if path == self.faulty and self.fault is not None:
            raise self.fault
        if path == "binary.png":
            raise UnicodeDecodeError("utf-8", b"\x89", 0, 1, "invalid start byte")
        return await super().load(path)


def _faulty_manager(items: dict[str, str], *, faulty: str = "", fault: Exception | None = None) -> ResourceManager:
    store = _FaultyStorage(items, faulty=faulty, fault=fault)
    return ResourceManager(store, on_evicted=lambda _eviction: None, held_by=_no_holders)


# --- the author's text at fault ---------------------------------------------


@pytest.mark.parametrize(
    ("content", "kwargs"),
    [
        ("{{ 1 / 0 }}", {}),
        ('{{ "a" + 1 }}', {}),
        ("{{ names | list_format }}", {"names": ["a", "b"]}),
        ("Hello {{ name }", {}),
        ("{{ x.y }}", {}),
    ],
)
async def test_an_inline_body_that_does_not_render_is_a_body_error(content: str, kwargs: dict[str, object]) -> None:
    manager, _ = _manager()
    with pytest.raises(TemplateBodyError) as caught:
        await manager.render_templated_text(TemplatedText(content=content, kwargs=kwargs))
    assert str(caught.value).startswith("is not a renderable template: ")


async def test_a_sandbox_refusal_is_a_body_error() -> None:
    manager, _ = _manager()
    with pytest.raises(TemplateBodyError):
        await manager.render_templated_text(TemplatedText(content="{{ ''.__class__.__mro__[1].__subclasses__() }}"))


async def test_a_stored_body_that_does_not_parse_is_a_body_error() -> None:
    manager, _ = _manager({"broken.j2": "Hello {{ name }"})
    with pytest.raises(TemplateBodyError) as caught:
        await manager.render_templated_text(TemplatedText(id="broken.j2"))
    assert str(caught.value).startswith("references stored id 'broken.j2', which could not be rendered: ")


async def test_a_missing_stored_id_is_not_found_naming_the_id() -> None:
    manager, _ = _manager()
    with pytest.raises(TemplateNotFoundError) as caught:
        await manager.render_templated_text(TemplatedText(id="absent.j2"))
    assert str(caught.value) == (
        "references stored id 'absent.j2', which could not be fetched: Template 'absent.j2' not found."
    )


async def test_an_exhausted_locale_chain_is_not_found() -> None:
    manager, _ = _manager()
    with pytest.raises(TemplateNotFoundError, match="has no variant for locale 'he-IL'"):
        await manager.render_templated_text(TemplatedText(id="absent.j2"), "he-IL")


@pytest.mark.parametrize("locale", [None, "he-IL"])
async def test_an_id_the_path_guard_refuses_is_not_found_with_the_guards_reason(locale: str | None) -> None:
    manager, _ = _manager()
    with pytest.raises(TemplateNotFoundError, match=re.escape("non-canonical store key '../x'")):
        await manager.render_templated_text(TemplatedText(id="../x"), locale)


async def test_an_include_naming_no_stored_template_is_not_found_and_the_fetched_id_could_not_be_rendered() -> None:
    manager, _ = _manager({"page.j2": "{% include 'gone.j2' %}"})
    with pytest.raises(TemplateNotFoundError) as caught:
        await manager.render_templated_text(TemplatedText(id="page.j2"))
    assert str(caught.value) == (
        "references stored id 'page.j2', which could not be rendered: its include 'gone.j2' is not stored"
    )


async def test_an_include_the_path_guard_refuses_is_not_found_and_the_fetched_id_could_not_be_rendered() -> None:
    manager, _ = _manager({"page.j2": "{% include '../x' %}"})
    with pytest.raises(TemplateNotFoundError) as caught:
        await manager.render_templated_text(TemplatedText(id="page.j2"))
    assert str(caught.value) == (
        "references stored id 'page.j2', which could not be rendered: non-canonical store key '../x': "
        "a key is '/'-separated names with no empty, '.' or '..' segment"
    )


async def test_a_stored_item_that_is_not_text_is_not_text() -> None:
    manager = _faulty_manager({"page.j2": "{% include 'binary.png' %}"})
    with pytest.raises(TemplateNotTextError) as caught:
        await manager.render_templated_text(TemplatedText(id="binary.png"))
    assert str(caught.value) == (
        "references stored id 'binary.png', which could not be fetched: "
        "Template 'binary.png' is not a text template: the stored object is not UTF-8 text."
    )
    with pytest.raises(TemplateNotTextError) as caught:
        await manager.render_templated_text(TemplatedText(id="page.j2"))
    assert str(caught.value) == (
        "references stored id 'page.j2', which could not be rendered: "
        "Template 'binary.png' is not a text template: the stored object is not UTF-8 text."
    )


async def test_kwargs_carrying_the_reserved_locale_key_are_a_body_error() -> None:
    manager, _ = _manager({"g.j2": "x"})
    for text in (
        TemplatedText(content="x", kwargs={LOCALE_CONTEXT_KEY: "he"}),
        TemplatedText(id="g.j2", kwargs={LOCALE_CONTEXT_KEY: "he"}),
    ):
        with pytest.raises(TemplateBodyError, match="reserved render variable"):
            await manager.render_templated_text(text)


# --- failures that are not the author's propagate as themselves ---------------


async def test_a_store_fault_inside_an_include_propagates_as_itself() -> None:
    fault = ConnectionError("store down")
    manager = _faulty_manager({"page.j2": "{% include 'dep.j2' %}"}, faulty="dep.j2", fault=fault)
    for text in (TemplatedText(id="page.j2"), TemplatedText(content="{% include 'dep.j2' %}")):
        with pytest.raises(ConnectionError) as caught:
            await manager.render_templated_text(text)
        assert caught.value is fault
        assert not isinstance(caught.value, TemplateRenderError)


async def test_a_store_fault_of_the_stored_text_itself_propagates_as_itself() -> None:
    manager = _faulty_manager({}, faulty="g.j2", fault=TimeoutError("slow store"))
    with pytest.raises(TimeoutError, match="slow store"):
        await manager.render_templated_text(TemplatedText(id="g.j2"))


async def test_a_storage_plugins_own_runtime_error_inside_an_include_propagates() -> None:
    manager = _faulty_manager(
        {"page.j2": "{% include 'dep.j2' %}"}, faulty="dep.j2", fault=RuntimeError("unexpected content type")
    )
    with pytest.raises(RuntimeError, match="unexpected content type"):
        await manager.render_templated_text(TemplatedText(id="page.j2"))


async def test_an_unconfigured_manager_rendering_an_include_raises_its_own_error() -> None:
    manager = ResourceManager(None, on_evicted=lambda _eviction: None, held_by=_no_holders)
    with pytest.raises(RuntimeError, match="no Storage provider registered"):
        await manager.render_templated_text(TemplatedText(content="{% include 'dep.j2' %}"))


async def test_a_loader_fault_never_marks_a_later_render_on_a_pooled_thread() -> None:
    manager = _faulty_manager({"page.j2": "{% include 'dep.j2' %}"}, faulty="dep.j2", fault=ConnectionError("down"))
    for _ in range(8):
        with pytest.raises(ConnectionError):
            await manager.render_templated_text(TemplatedText(id="page.j2"))
        with pytest.raises(TemplateBodyError):
            await manager.render_templated_text(TemplatedText(content="{{ 1 / 0 }}"))
