"""Shared fixtures: a stub ``tai42_app`` bound before the plugin is imported, and a manager whose HTTP client
talks to the in-memory fake server.

``tai42_monitoring_langfuse.register`` registers via ``tai42_app`` at import time, so a recording stub app is bound
here first. The ``manager`` fixture patches ``active_client`` to return a client over the fake's transport, never
reaching the network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from tai42_contract.app import tai42_app
from tai42_kit.settings import reset_all_settings

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.project import LangfuseProject
from tai42_monitoring_langfuse.reader import LangfuseReader

from ._fake_langfuse import FakeLangfuse


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
def project() -> LangfuseProject:
    return LangfuseProject(public_key="pk-test", secret_key="sk-test", host="http://localhost")


@pytest.fixture
def fake() -> FakeLangfuse:
    return FakeLangfuse()


@pytest.fixture
def manager(
    project: LangfuseProject, fake: FakeLangfuse, monkeypatch: pytest.MonkeyPatch
) -> Iterator[LangfuseClientManager]:
    mgr = LangfuseClientManager(project)
    with httpx.Client(
        transport=httpx.MockTransport(fake.handle),
        base_url="http://langfuse.test",
        auth=httpx.BasicAuth(project.public_key, project.secret_key),
    ) as client:
        monkeypatch.setattr(mgr, "active_client", lambda: client)
        yield mgr


@pytest.fixture
def reader(manager: LangfuseClientManager) -> LangfuseReader:
    return LangfuseReader(manager)
