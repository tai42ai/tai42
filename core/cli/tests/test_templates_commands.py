"""The ``tai templates`` command group, exercised against a fake ``/api/*template*``
server that models the store's file/directory id space.

The composed path a live upload takes: an id occupied by a directory that still
holds templates is refused, deleting the last child frees the id, and a re-upload
at the freed id then succeeds.
"""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


class _FakeStore:
    """A minimal template store keyed like the real backends: one id space where a
    file id may not also name a directory that still holds templates."""

    def __init__(self, keys: dict[str, str]) -> None:
        self.keys = dict(keys)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/upload-template":
            path = body["path"]
            under = sorted(k for k in self.keys if k.startswith(f"{path}/"))
            if under:
                return error_response(
                    f"cannot upload template {path!r}: templates exist under that path: "
                    f"{', '.join(repr(k) for k in under)}; delete them first",
                    409,
                )
            self.keys[path] = body["content"]
            return data_response({"path": path, "uploaded": True})
        if request.url.path == "/api/delete-template":
            self.keys.pop(body["path"], None)
            return data_response({"path": body["path"], "deleted": True})
        raise AssertionError(f"unexpected request to {request.url.path}")


def test_delete_then_upload_at_freed_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    store = _FakeStore({"a/b/c.j2": "child"})
    source = tmp_path / "new.j2"
    source.write_text("i am the new a/b", encoding="utf-8")

    # The id "a/b" is occupied by a directory holding "a/b/c.j2": the upload is refused.
    refused = run_cli(monkeypatch, store, ["templates", "upload", "a/b", "--file", str(source)])
    assert refused.exit_code != 0
    assert "delete them first" in refused.output

    # Delete the last child, freeing the id.
    freed = run_cli(monkeypatch, store, ["templates", "delete", "a/b/c.j2"])
    assert freed.exit_code == 0, freed.output

    # The re-upload at the freed id now succeeds.
    uploaded = run_cli(monkeypatch, store, ["templates", "upload", "a/b", "--file", str(source)])
    assert uploaded.exit_code == 0, uploaded.output
    assert store.keys["a/b"] == "i am the new a/b"
