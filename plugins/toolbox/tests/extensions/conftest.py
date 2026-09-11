"""Shared fixtures for the tool-extension tests.

A toolbox module registers its tool extension through ``tai42_app`` at import time. These fixtures
bind capturing or behavior-providing fake apps so a test can assert the registration or drive a
composed callable, restoring a null app afterwards.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Iterator
from types import ModuleType
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind


class _NullTools:
    def tool(self, func: Callable[..., Any] | None = None, /, *args: Any, **kwargs: Any) -> Any:
        if callable(func):
            return func

        def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
            return f

        return decorate


class _NullExtensions:
    def extension(
        self,
        f: Callable[..., Any] | None = None,
        *,
        kind: Any = None,
        name: str | None = None,
        requires_body_locality: bool = False,
    ) -> Any:
        if callable(f):
            return f

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn

        return decorate


class _NullApp:
    tools = _NullTools()
    extensions = _NullExtensions()


class CapturingExtensions:
    """Records every ``extension`` registration and returns the factory unchanged,
    mirroring the real registrar so a reloaded module registers here."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, ExtensionKind, bool]] = []

    def extension(
        self,
        f: Callable[..., Any] | None = None,
        *,
        kind: ExtensionKind,
        name: str | None = None,
        requires_body_locality: bool = False,
    ) -> Any:
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.registered.append((name or fn.__name__, kind, requires_body_locality))
            return fn

        if callable(f):
            return decorate(f)
        return decorate


class CapturingApp:
    def __init__(self) -> None:
        self.tools = _NullTools()
        self.extensions = CapturingExtensions()


class FakeTools:
    """A tools facet whose ``run_tool`` and ``tool_title`` are supplied per test."""

    def __init__(
        self,
        run_tool: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
        tool_title: Callable[[Callable[..., Any]], str] | None = None,
    ) -> None:
        self._run_tool = run_tool
        self._tool_title = tool_title

    async def run_tool(self, key: str, arguments: dict[str, Any]) -> Any:
        if self._run_tool is None:
            raise AssertionError("run_tool was not provided to FakeTools")
        return await self._run_tool(key, arguments)

    def tool_title(self, func: Callable[..., Any]) -> str:
        if self._tool_title is None:
            raise AssertionError("tool_title was not provided to FakeTools")
        return self._tool_title(func)


class _FakeResourceManager:
    """Renders a jq slot: inline ``content`` verbatim, or a stored ``id`` from a per-id map —
    an unmapped id is the loud not-found the real manager raises."""

    def __init__(self, by_id: dict[str, str] | None = None) -> None:
        self._by_id = by_id or {}

    async def render_templated_text(self, text: Any, locale: str | None = None) -> str:
        if text.id is not None:
            if text.id not in self._by_id:
                raise KeyError(f"no stored resource {text.id!r}")
            return self._by_id[text.id]
        assert text.content is not None
        return text.content


class _FakeStorage:
    def __init__(self, resource_manager: _FakeResourceManager) -> None:
        self.resource_manager = resource_manager


class FakeApp:
    def __init__(self, tools: FakeTools, by_id: dict[str, str] | None = None) -> None:
        self.tools = tools
        self.extensions = _NullExtensions()
        self.storage = _FakeStorage(_FakeResourceManager(by_id))


@pytest.fixture(autouse=True)
def restore_null_app() -> Iterator[None]:
    """Rebind a stateless null app after every test so rebinding within a test
    never leaks into the next."""
    yield
    tai42_app.bind(_NullApp())


@pytest.fixture
def bind_fake_app() -> Callable[..., None]:
    def _bind(tools: FakeTools, by_id: dict[str, str] | None = None) -> None:
        tai42_app.bind(FakeApp(tools, by_id))

    return _bind


@pytest.fixture
def capture_registration() -> Callable[[ModuleType], list[tuple[str, ExtensionKind, bool]]]:
    """Re-run a module's import body under a capturing app and return what it
    registered, as ``[(name, kind, requires_body_locality)]``."""

    def _capture(module: ModuleType) -> list[tuple[str, ExtensionKind, bool]]:
        app = CapturingApp()
        tai42_app.bind(app)
        importlib.reload(module)
        return app.extensions.registered

    return _capture
