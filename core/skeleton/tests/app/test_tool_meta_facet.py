"""The in-process tool-metadata patch facet door (``app.tool_meta.patch``).

Driven over the true :class:`PostgresToolMetaStore` on the stateful fake Postgres (the
``pg`` fixture) — so the in-process door runs the SAME operation as the HTTP PATCH door
(identical validation and record shape), proven by comparing the two doors' outputs and
by the shared unknown-folder refusal.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from starlette.requests import Request

import tai42_skeleton.tool_meta.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.routers import tool_meta as router

from ..tool_meta.conftest import FakeToolMetaPg, make_pg_ctx


@pytest.fixture(autouse=True)
def _tool_meta_store_configured(monkeypatch):
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "x")


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> FakeToolMetaPg:
    fake = FakeToolMetaPg()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(fake))
    return fake


def _run(coro_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coro_factory())


def _request(method: str, path: str, *, body: Any = None, **path_params: str) -> Request:
    payload = b"" if body is None else json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "path_params": path_params,
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


def _data(resp) -> Any:
    return json.loads(bytes(resp.body))["data"]


async def _create_folder(name: str) -> str:
    resp = await router.create_folder(_request("POST", "/api/tool-meta/folders", body={"name": name}))
    return _data(resp)["id"]


def test_patch_sets_tags_and_folder(pg: FakeToolMetaPg) -> None:
    async def run() -> None:
        folder_id = await _create_folder("bucket")
        row = await instance.app.tool_meta.patch("weather", tags=["a", "b"], folder_id=folder_id)
        assert row["tags"] == ["a", "b"]
        assert row["folder_id"] == folder_id

    _run(run)


def test_patch_tags_none_leaves_tags_untouched(pg: FakeToolMetaPg) -> None:
    async def run() -> None:
        await instance.app.tool_meta.patch("weather", tags=["keep"])
        folder_id = await _create_folder("bucket")
        # A folder-only patch leaves the tag set intact.
        row = await instance.app.tool_meta.patch("weather", folder_id=folder_id)
        assert row["tags"] == ["keep"]
        assert row["folder_id"] == folder_id

    _run(run)


def test_patch_tags_replace_the_whole_set(pg: FakeToolMetaPg) -> None:
    async def run() -> None:
        await instance.app.tool_meta.patch("weather", tags=["a", "b"])
        row = await instance.app.tool_meta.patch("weather", tags=["c"])
        assert row["tags"] == ["c"]

    _run(run)


def test_patch_unknown_folder_raises_like_http_door(pg: FakeToolMetaPg) -> None:
    async def run() -> None:
        with pytest.raises(BadRequestError) as exc_info:
            await instance.app.tool_meta.patch("weather", folder_id="nope")
        assert "does not exist" in exc_info.value.message

    _run(run)


def test_patch_response_shape_equals_http_door(pg: FakeToolMetaPg) -> None:
    async def run() -> None:
        facet_row = await instance.app.tool_meta.patch("weather", tags=["a"])
        http_row = _data(
            await router.upsert_tool_meta(
                _request("PATCH", "/api/tool-meta/tools/echo", body={"tags": ["a"]}, tool_name="echo")
            )
        )
        assert set(facet_row) == set(http_row)

    _run(run)
