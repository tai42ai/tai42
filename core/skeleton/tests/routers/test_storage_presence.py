"""Storage presence: the always-mounted deployment fact ``GET /api/storage``.

The presence read reports provider identity, or ``present: false`` (a ``200``) when
none is registered, over the unchanged ``storage_info`` operation. It lives in the
core presence router so it is answerable in every deployment, independent of the
optional storage management surface. Handlers are driven directly; the provider is
installed by setting the process app's storage-registry provider to a fake, and the
absent case sets it to ``None``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from tai42_contract.storage import ObjectStat, Storage

from tai42_skeleton.app import instance
from tai42_skeleton.routers import storage_presence as router


class _FakeStorage(Storage):
    """A minimal in-memory content store — enough for the presence read to report
    identity."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def load(self, path: str) -> str:
        return self.objects[path].decode("utf-8")

    async def list(self) -> list[str]:
        return list(self.objects)

    async def upload(self, path: str, content: str) -> None:
        self.objects[path] = content.encode("utf-8")

    async def delete(self, path: str) -> None:
        del self.objects[path]

    async def delete_dir(self, path: str) -> None:
        raise NotImplementedError

    async def stat(self, path: str) -> ObjectStat:
        return ObjectStat(content_type=None)


@pytest.fixture
def install(monkeypatch):
    def _install(provider: Storage | None) -> Storage | None:
        monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
        return provider

    return _install


def _req(**path_params) -> Request:
    return cast(Request, SimpleNamespace(path_params=path_params))


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


async def test_info_present(install):
    fake = install(_FakeStorage())
    resp = await router.storage_info(_req())
    assert resp.status_code == 200
    data = _json(resp)["data"]
    assert data["present"] is True
    assert data["provider"] == "_FakeStorage"
    assert data["module"] == type(fake).__module__


async def test_info_absent_is_200_present_false(install):
    install(None)
    resp = await router.storage_info(_req())
    assert resp.status_code == 200
    assert _json(resp) == {"data": {"present": False, "provider": None, "module": None}}
