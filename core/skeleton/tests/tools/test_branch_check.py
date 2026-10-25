"""The dry-run combo check: ``check_combo`` binds a combo once against the live registry, mounts nothing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.tools.binding.errors import ExtensionComboRefusedError

from .._fakes.combo_check_app import booted_check_app, clean_server, factory_calls

pytestmark = pytest.mark.usefixtures(clean_server.__name__)


def _calls() -> list[str]:
    return factory_calls()


def _booted(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def run() -> None:
        async with booted_check_app():
            await body()

    asyncio.run(run())


async def _surface() -> tuple[set[str], dict[str, str], list[list[Any]]]:
    tools = set(await app.tools.get_tools())
    links = dict(app._tool_registry._extend_tools)
    combos = list(app._tool_registry.tool_extensions_iterator("echo"))
    return tools, links, combos


def _refusal(tool: str, combo: list[Any]) -> ExtensionComboRefusedError:
    with pytest.raises(ExtensionComboRefusedError) as caught:
        app.extensions.check_combo(tool, combo)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    return caught.value


_LABELLED_REQUIRED = (
    "extension 'labelled' config: value does not match schema at $: "
    "breaks the schema's 'required' rule: 'label' (the value is not shown)"
)


def test_a_factory_refusal_names_the_extension():
    async def body() -> None:
        before = await _surface()
        exc = _refusal("echo", [{"name": "refusing", "config": {"label": "x"}}])
        assert str(exc) == "extension 'refusing' on tool 'echo' cannot bind: bad label"
        assert await _surface() == before

    _booted(body)


def test_the_refusing_element_of_a_stack_is_named():
    async def body() -> None:
        exc = _refusal(
            "echo", [{"name": "labelled", "config": {"label": "ok"}}, {"name": "refusing", "config": {"label": "x"}}]
        )
        assert str(exc) == "extension 'refusing' on tool 'echo' cannot bind: bad label"

    _booted(body)


def test_an_empty_factory_message_reads_as_its_type_name():
    async def body() -> None:
        exc = _refusal("echo", [{"name": "refusing", "config": {"label": "blank"}}])
        assert str(exc) == "extension 'refusing' on tool 'echo' cannot bind: ValueError"

    _booted(body)


def test_locality_order_and_same_name_return_are_refused_in_the_binds_words():
    async def body() -> None:
        before = await _surface()
        exc = _refusal("echo", ["relocx", "localw"])
        assert str(exc).startswith(
            "extension 'localw' on tool 'echo' cannot bind: tool 'echo': extension 'localw' requires body locality"
        )
        exc = _refusal("echo", ["sameret"])
        assert str(exc) == (
            "extension 'sameret' on tool 'echo' cannot bind: Extension 'sameret' returned the same name 'echo' "
            "as the original tool. Extensions must return a new name to create a branch."
        )
        assert await _surface() == before

    _booted(body)


def test_a_passing_combo_runs_each_factory_once_and_mounts_nothing():
    async def body() -> None:
        before = await _surface()
        _calls().clear()
        app.extensions.check_combo("echo", [{"name": "labelled", "config": {"label": "ok"}}, "localw"])
        assert _calls() == ["labelled", "localw"]
        assert await _surface() == before

    _booted(body)


def test_settings_are_checked_without_a_bound_base_and_beside_an_unregistered_extension():
    async def body() -> None:
        _calls().clear()
        exc = _refusal("fresh", [{"name": "labelled", "config": {}}])
        assert str(exc) == f"extension 'labelled' on tool 'fresh' cannot bind: {_LABELLED_REQUIRED}"
        exc = _refusal("echo", [{"name": "labelled", "config": {}}, "not_loaded"])
        assert str(exc) == f"extension 'labelled' on tool 'echo' cannot bind: {_LABELLED_REQUIRED}"
        app.extensions.check_combo("fresh", [{"name": "refusing", "config": {"label": "x"}}])
        app.extensions.check_combo("echo", [{"name": "refusing", "config": {"label": "x"}}, "not_loaded"])
        assert _calls() == []

    _booted(body)


def test_cardinality_is_refused_without_a_bound_base():
    async def body() -> None:
        _calls().clear()
        exc = _refusal("fresh", ["relocx", "relocy"])
        assert str(exc).startswith("extensions on tool 'fresh' cannot bind: Only one ")
        assert _calls() == []

    _booted(body)


def test_a_given_base_is_used_in_place_of_the_served_tool():
    async def body() -> None:
        served = await app.tools.get_tool("echo")
        _calls().clear()
        with pytest.raises(ExtensionComboRefusedError) as caught:
            app.extensions.check_combo("p", [{"name": "refusing", "config": {"label": "x"}}], base=served)
        assert str(caught.value) == "extension 'refusing' on tool 'p' cannot bind: bad label"
        assert _calls() == ["refusing"]

    _booted(body)
