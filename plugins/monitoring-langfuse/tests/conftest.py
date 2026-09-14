"""Shared fixtures: a stub ``tai42_app`` bound before the plugin is imported, and
a manager whose Langfuse client is a mock.

``tai42_monitoring_langfuse.register`` registers via ``tai42_app`` at import time,
so a recording stub app is bound here first. The ``manager`` fixture patches
``active_client`` to return ``mock_client``, never constructing a real client.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
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


@pytest.fixture
def obs() -> Callable[..., SimpleNamespace]:
    """Factory for a Langfuse observation row (SDK snake_case attributes)."""

    def _make(**kw: Any) -> SimpleNamespace:
        base = {
            "id": "o",
            "trace_id": "t",
            "parent_observation_id": None,
            "type": "SPAN",
            "name": "node",
            "input": None,
            "output": None,
            "metadata": None,
            "start_time": datetime(2026, 1, 1, tzinfo=UTC),
            "end_time": None,
        }
        base.update(kw)
        return SimpleNamespace(**base)

    return _make


@pytest.fixture
def obs_page() -> Callable[..., SimpleNamespace]:
    """Factory for a paged observations response (``data`` + ``meta``)."""

    def _make(data: list[Any], page: int = 1, total_pages: int = 1) -> SimpleNamespace:
        return SimpleNamespace(data=data, meta=SimpleNamespace(page=page, total_pages=total_pages))

    return _make


@pytest.fixture
def trace_body() -> Callable[..., SimpleNamespace]:
    """Factory for a complete trace body (``model_dump`` payload for get_trace)."""

    def _make(**kw: Any) -> SimpleNamespace:
        base = {
            "id": "t1",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "tags": ["run:7"],
            "output": {"r": 1},
            "observations": [],
        }
        base.update(kw)
        return SimpleNamespace(model_dump=lambda: base)

    return _make


@pytest.fixture
def trace_row() -> Callable[..., SimpleNamespace]:
    """Factory for a trace.list summary row (``TraceWithDetails``), snake_case as
    the SDK model exposes it: the io + metrics field groups, no observation bodies."""

    def _make(**kw: Any) -> SimpleNamespace:
        base = {
            "id": "t1",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "name": None,
            "tags": [],
            "metadata": None,
            "input": None,
            "output": None,
            "latency": None,
            "total_cost": None,
        }
        base.update(kw)
        return SimpleNamespace(**base)

    return _make


@pytest.fixture
def list_returns(mock_client: MagicMock) -> Callable[..., None]:
    """Route ``trace.list`` to return the given summary rows."""

    def _apply(rows: list[Any], *, total_pages: int = 1) -> None:
        mock_client.api.trace.list.return_value = SimpleNamespace(
            data=rows, meta=SimpleNamespace(total_pages=total_pages)
        )

    return _apply


@pytest.fixture
def route_metrics(mock_client: MagicMock) -> Callable[..., None]:
    """Route ``metrics_v1.metrics``: the ranking query (carries ``orderBy``) versus
    the token join query (``totalTokens``, no ``orderBy``)."""

    def _apply(*, ranking: list[Any] | None = None, tokens: dict[str, Any] | None = None) -> None:
        ranking = ranking if ranking is not None else []
        tokens = tokens if tokens is not None else {}

        def _call(query: str, request_options: Any = None) -> SimpleNamespace:
            q = json.loads(query)
            if "orderBy" in q:
                return SimpleNamespace(data=list(ranking))
            return SimpleNamespace(data=[{"id": k, "sum_totalTokens": v} for k, v in tokens.items()])

        mock_client.api.legacy.metrics_v1.metrics.side_effect = _call

    return _apply


@pytest.fixture
def errors_for(
    mock_client: MagicMock,
    obs: Callable[..., SimpleNamespace],
    obs_page: Callable[..., SimpleNamespace],
) -> Callable[..., None]:
    """Route the error-status ``get_many`` to observations for the given trace ids."""

    def _apply(trace_ids: tuple[str, ...] = (), *, total_pages: int = 1) -> None:
        rows = [obs(id=f"err-{i}", trace_id=tid, level="ERROR") for i, tid in enumerate(trace_ids)]
        mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(rows, total_pages=total_pages)

    return _apply


@pytest.fixture
def metric_query(mock_client: MagicMock) -> Callable[[], dict[str, Any]]:
    """The most recent ranking metrics query (the one carrying ``orderBy``) —
    distinct from the token-join query issued on the same metrics endpoint."""

    def _get() -> dict[str, Any]:
        for call in reversed(mock_client.api.legacy.metrics_v1.metrics.call_args_list):
            q = json.loads(call.kwargs["query"])
            if "orderBy" in q:
                return q
        raise AssertionError("no ranking metrics query was issued")

    return _get


@pytest.fixture
def token_query(mock_client: MagicMock) -> Callable[[], dict[str, Any]]:
    """The token-join metrics query (the one with no ``orderBy``)."""

    def _get() -> dict[str, Any]:
        for call in reversed(mock_client.api.legacy.metrics_v1.metrics.call_args_list):
            q = json.loads(call.kwargs["query"])
            if "orderBy" not in q:
                return q
        raise AssertionError("no token-join metrics query was issued")

    return _get
