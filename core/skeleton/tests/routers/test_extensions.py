"""Extension router: the flat ``{name, kind, multiple, config_schema}`` listing for the picker."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind

from tai42_skeleton.extensions.registry import ExtensionRegistry
from tai42_skeleton.operations.response_models_group_c import ExtensionListing
from tai42_skeleton.routers import extensions as router


def _req() -> Request:
    return cast(Request, SimpleNamespace(path_params={}))


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


class _FakeExtensions:
    def __init__(self, items):
        self._items = items

    def available_extensions(self):
        return self._items


@pytest.fixture
def install(monkeypatch):
    def _install(items):
        monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(extensions=_FakeExtensions(items)))

    return _install


_LABEL_SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}


def _plain(func, name, desc):
    return func


def _labelled(func, name, desc, config):
    return func


async def test_list_extensions(install):
    registry = ExtensionRegistry(frozenset())
    registry.register_extension(_plain, ExtensionKind.WRAPPER, "plain")
    registry.register_extension(_labelled, ExtensionKind.BACKEND, "labelled", config_schema=_LABEL_SCHEMA)
    install(registry.available_extensions())
    resp = await router.list_extensions(_req())
    assert resp.status_code == 200
    body = _json(resp)
    assert body == {
        "data": [
            {"name": "labelled", "kind": "backend", "multiple": False, "config_schema": _LABEL_SCHEMA},
            {"name": "plain", "kind": "wrapper", "multiple": True, "config_schema": None},
        ]
    }
    # The served items are the operation's declared response shape.
    assert ExtensionListing.model_validate(body["data"]).model_dump() == body["data"]


async def test_list_extensions_empty(install):
    install([])
    resp = await router.list_extensions(_req())
    assert _json(resp) == {"data": []}
