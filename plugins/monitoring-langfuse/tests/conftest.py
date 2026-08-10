"""Shared fixtures: a stub ``tai42_app`` bound before the plugin is imported, and
a manager whose Langfuse client is a mock.

``tai42_monitoring_langfuse.register`` registers via ``tai42_app`` at import time,
so a recording stub app is bound here first. The ``manager`` fixture patches
``active_client`` to return ``mock_client``, never constructing a real client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import ProjectConfig
from tai42_kit.settings import reset_all_settings

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager


class _StubMonitoring:
    """Records the builders the plugin registers, without building them."""

    def __init__(self) -> None:
        self.registered_builders: list[Callable[[], Any]] = []

    def register_monitoring(self, builder: Callable[[], Any] | None = None) -> Any:
        if builder is None:
            return self.register_monitoring
        self.registered_builders.append(builder)
        return builder


class _StubApp:
    def __init__(self) -> None:
        self.monitoring = _StubMonitoring()


_stub_app = _StubApp()
tai42_app.bind(_stub_app)


@pytest.fixture
def stub_monitoring() -> _StubMonitoring:
    return _stub_app.monitoring


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Drop every cached settings singleton around each test, so env
    manipulated via monkeypatch never leaks through the settings cache."""
    reset_all_settings()
    yield
    reset_all_settings()


@pytest.fixture
def project() -> ProjectConfig:
    return ProjectConfig(public_key="pk-test", secret_key="sk-test", host="http://localhost")


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock(name="LangfuseClient")


@pytest.fixture
def manager(project: ProjectConfig, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> LangfuseClientManager:
    mgr = LangfuseClientManager([project], project.public_key)
    # Never construct a real Langfuse client in unit tests.
    monkeypatch.setattr(mgr, "_ensure_built", lambda: None)
    monkeypatch.setattr(mgr, "active_client", lambda: mock_client)
    return mgr
