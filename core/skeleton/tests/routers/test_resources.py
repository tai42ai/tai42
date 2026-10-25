"""Resources router: load a stored resource by id, with the text/media/404/400
surface and the adapter's media serialization over HTTP."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp.utilities.types import Image
from starlette.requests import Request

import tai42_skeleton.operations.resources as resources_ops
import tai42_skeleton.routers.resources as router
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template.media import MediaBlock

from .._fakes.content_fixtures import (
    FILES_INSTALL_LINE,
    LOCKED_PDF_MESSAGE,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    locked_pdf,
)
from .._fakes.recording_storage import RecordingStorage


def _req(body: dict | None = None, *, query: dict | None = None) -> Request:
    async def _json() -> Any:
        return body or {}

    return cast(Request, SimpleNamespace(json=_json, path_params={}, query_params=query or {}))


def _data(resp):
    return json.loads(bytes(resp.body))


class _ResourceManager:
    def __init__(self, *, loaded: str | MediaBlock = "", rendered: str = "", raise_exc: Exception | None = None):
        self._loaded = loaded
        self._rendered = rendered
        self._raise = raise_exc

    async def load_file(self, source: str) -> str | MediaBlock:
        if self._raise is not None:
            raise self._raise
        return self._loaded

    async def render_templated_text(self, text, locale=None) -> str:
        return self._rendered


@pytest.fixture
def bind(monkeypatch):
    def _bind(manager: _ResourceManager) -> _ResourceManager:
        fake_app = SimpleNamespace(storage=SimpleNamespace(resource_manager=manager))
        monkeypatch.setattr(resources_ops, "tai42_app", fake_app)
        return manager

    return _bind


async def test_get_text_unrendered(bind):
    bind(_ResourceManager(loaded="raw {{ body }}"))
    resp = await router.get_resource_by_id(_req({"resource_id": "doc.txt"}))
    assert resp.status_code == 200
    assert _data(resp)["data"] == "raw {{ body }}"


async def test_get_route_fetches_as_is_from_query(bind):
    # The read-classed GET door parses ``resource_id`` from the query string (no body)
    # and returns the stored resource as-is. The manager renders to "", so getting the
    # raw loaded text back proves no render happened.
    bind(_ResourceManager(loaded="raw {{ body }}", rendered="RENDERED"))
    resp = await router.fetch_resource(_req(query={"resource_id": "doc.txt"}))
    assert resp.status_code == 200
    assert _data(resp)["data"] == "raw {{ body }}"


async def test_get_renders_with_kwargs(bind):
    bind(_ResourceManager(loaded="Hello {{ name }}", rendered="Hello Ada"))
    resp = await router.get_resource_by_id(_req({"resource_id": "greet.j2", "kwargs": {"name": "Ada"}}))
    assert _data(resp)["data"] == "Hello Ada"


async def test_get_media_serializes_over_http(bind):
    # The adapter converts a fastmcp media return to its JSON-native MCP wire block.
    bind(_ResourceManager(loaded=Image(data=b"\x89PNG\r\n", format="png")))
    resp = await router.get_resource_by_id(_req({"resource_id": "logo.png"}))
    assert resp.status_code == 200
    block = _data(resp)["data"]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert "data" in block  # base64 payload


async def test_get_missing_is_404(bind):
    bind(_ResourceManager(raise_exc=FileNotFoundError("gone")))
    resp = await router.get_resource_by_id(_req({"resource_id": "gone.txt"}))
    assert resp.status_code == 404
    assert "not found" in _data(resp)["error"]


async def test_render_media_is_400(bind):
    bind(_ResourceManager(loaded=Image(data=b"\x89PNG", format="png")))
    resp = await router.get_resource_by_id(_req({"resource_id": "logo.png", "kwargs": {"x": 1}}))
    assert resp.status_code == 400
    assert "Cannot render media" in _data(resp)["error"]


def _bind_recording(monkeypatch, objects: dict[str, bytes]) -> RecordingStorage:
    provider = RecordingStorage({key: (data, None) for key, data in objects.items()})
    manager = ResourceManager(provider, on_evicted=lambda _eviction: None)
    monkeypatch.setattr(resources_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=manager)))
    return provider


async def _both_doors(resource_id: str):
    return [
        await router.fetch_resource(_req(query={"resource_id": resource_id})),
        await router.get_resource_by_id(_req({"resource_id": resource_id})),
    ]


async def test_an_extractors_failure_is_400_with_its_text_on_both_doors(monkeypatch):
    _bind_recording(monkeypatch, {"docs/locked.pdf": locked_pdf(), "data/scores.csv": RAGGED_CSV})
    for resp in await _both_doors("docs/locked.pdf"):
        assert resp.status_code == 400
        assert _data(resp) == {"error": f"resource 'docs/locked.pdf' cannot be loaded: {LOCKED_PDF_MESSAGE}"}
    for resp in await _both_doors("data/scores.csv"):
        assert resp.status_code == 400
        assert _data(resp) == {"error": f"resource 'data/scores.csv' cannot be loaded: {RAGGED_CSV_MESSAGE}"}


@pytest.mark.usefixtures("files_extra_missing")
async def test_without_the_files_extra_both_doors_answer_501(monkeypatch):
    _bind_recording(monkeypatch, {"a.txt": b"hello"})
    for resp in await _both_doors("a.txt"):
        assert resp.status_code == 501
        assert _data(resp) == {"error": FILES_INSTALL_LINE, "code": "files-not-configured"}
