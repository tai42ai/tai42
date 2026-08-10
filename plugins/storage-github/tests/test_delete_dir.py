"""``delete_dir``: the root guard, per-file deletion, and error propagation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import HTTPStatusError, Request

from tai42_storage_github import GithubStorage
from tests.conftest import make_response


@pytest.mark.parametrize("root", ["", "/", ".", "  ", "//", "a/..", "./x/../..", "../x", "/.."])
async def test_delete_dir_root_escape_refused(root):
    with pytest.raises(ValueError, match="storage root"):
        await GithubStorage().delete_dir(root)


async def test_delete_dir_deletes_each_file(monkeypatch):
    store = GithubStorage()
    list_blobs = AsyncMock(return_value=["d/a.j2", "d/b.j2"])
    monkeypatch.setattr(store, "_list_blobs", list_blobs)
    deleted: list[str] = []
    monkeypatch.setattr(store, "delete", AsyncMock(side_effect=lambda p: deleted.append(p)))

    await store.delete_dir("d")

    assert deleted == ["d/a.j2", "d/b.j2"]
    # The prefix passed to the listing carries a trailing slash so "d" never
    # matches sibling "dd/...".
    assert list_blobs.call_args.args[0] == "d/"


async def test_delete_dir_keeps_existing_trailing_slash(monkeypatch):
    store = GithubStorage()
    list_blobs = AsyncMock(return_value=["d/a.j2"])
    monkeypatch.setattr(store, "_list_blobs", list_blobs)
    monkeypatch.setattr(store, "delete", AsyncMock())

    await store.delete_dir("d/")

    assert list_blobs.call_args.args[0] == "d/"


async def test_delete_dir_empty_raises_filenotfound(monkeypatch):
    store = GithubStorage()
    monkeypatch.setattr(store, "_list_blobs", AsyncMock(return_value=[]))

    with pytest.raises(FileNotFoundError):
        await store.delete_dir("d")


async def test_delete_dir_tolerates_vanished_file(monkeypatch):
    store = GithubStorage()
    monkeypatch.setattr(store, "_list_blobs", AsyncMock(return_value=["d/a.j2", "d/b.j2"]))
    deleted: list[str] = []

    async def _delete(p):
        if p == "d/a.j2":
            raise FileNotFoundError("vanished")
        deleted.append(p)

    monkeypatch.setattr(store, "delete", AsyncMock(side_effect=_delete))

    await store.delete_dir("d")

    assert deleted == ["d/b.j2"]


async def test_delete_dir_real_failure_propagates(monkeypatch):
    store = GithubStorage()
    monkeypatch.setattr(store, "_list_blobs", AsyncMock(return_value=["d/a.j2", "d/b.j2"]))
    request = Request("DELETE", "https://example.invalid")
    err = HTTPStatusError("boom", request=request, response=make_response(status=500))
    monkeypatch.setattr(store, "delete", AsyncMock(side_effect=err))

    with pytest.raises(HTTPStatusError):
        await store.delete_dir("d")
