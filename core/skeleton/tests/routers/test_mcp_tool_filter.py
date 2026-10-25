"""The MCP tool-selection doors over HTTP: the body they accept, the keys they write, their methods."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from tai42_contract.presets import PresetBody

from tai42_skeleton.app import instance
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.routers import manifest as router

from .._fakes.bus import FakeBus
from ..config.fake_pipeline import FakeConfigStore, FakeReloadAdmin, _service, _with_bus

_ENTRY = {
    "title": "acme",
    "config": {"type": "streamable_http", "url": "https://example.com/mcp"},
    "include": [],
    "exclude": [],
    "extensions": {"acme_send": [["cache"]]},
}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeConfigStore]:
    store = FakeConfigStore(manifest={"mcp": [dict(_ENTRY)]})
    bus = FakeBus(origin="serve-test")
    _with_bus(monkeypatch)
    monkeypatch.setattr(instance.app, "_bus", bus)
    admin = FakeReloadAdmin(served={"acme": ["acme_send", "acme_read"]})
    monkeypatch.setattr(ConfigService, "from_app", classmethod(lambda cls: _service(store, admin=admin, bus=bus)[0]))

    async def no_bodies(_facet: Any) -> dict[str, PresetBody]:
        return {}

    monkeypatch.setattr(type(instance.app.presets), "list_active_bodies", no_bodies)
    instance.app._rename_referee_registry.reset()
    yield store
    instance.app._rename_referee_registry.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(
        Starlette(
            routes=[
                Route("/api/mcp-config/entries/{title}/tool-filter", router.set_mcp_tool_filter, methods=["PUT"]),
                Route("/api/mcp-status/{title}/advertised-tools", router.list_mcp_advertised_tools, methods=["POST"]),
            ]
        )
    )


def test_the_tool_filter_writes_only_the_two_lists(store: FakeConfigStore, client: TestClient) -> None:
    response = client.put("/api/mcp-config/entries/acme/tool-filter", json={"include": ["acme_send"]})

    assert response.status_code == 200
    assert store.manifest["mcp"] == [{**_ENTRY, "include": ["acme_send"], "exclude": []}]


def test_the_tool_filter_on_an_unknown_title_is_404(store: FakeConfigStore, client: TestClient) -> None:
    response = client.put("/api/mcp-config/entries/ghost/tool-filter", json={"include": ["ghost_send"]})

    assert response.status_code == 404
    assert response.json()["error"] == "unknown mcp entry title: 'ghost'"
    assert store.persisted == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"include": ["acme_send"], "exclude": ["acme_read"]}, id="both-lists"),
        pytest.param({"include": [""]}, id="empty-name"),
        pytest.param({"exclude": ["acme_send", "acme_send"]}, id="duplicate-name"),
        pytest.param({"include": "acme_send"}, id="not-a-list"),
    ],
)
def test_the_tool_filter_refuses_a_malformed_body_with_422(
    store: FakeConfigStore, client: TestClient, body: dict
) -> None:
    response = client.put("/api/mcp-config/entries/acme/tool-filter", json=body)

    assert response.status_code == 422
    assert "acme_send" not in response.text
    assert store.persisted == []


def test_the_advertised_tools_read_serves_post_only(client: TestClient) -> None:
    assert client.get("/api/mcp-status/acme/advertised-tools").status_code == 405
    assert client.get("/api/mcp-config/entries/acme/tool-filter").status_code == 405
