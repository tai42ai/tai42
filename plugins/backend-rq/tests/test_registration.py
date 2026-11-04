"""Import-time registration: the three BACKEND extensions, the backend class itself, and the
views RQ cannot answer refusing loudly."""

from __future__ import annotations

import pytest
from tai42_contract.extensions import ExtensionKind
from tai42_kit.backend import UnsupportedBackendTool

import tai42_backend_rq
from tai42_backend_rq.backend import RqBackend

# The tools RQ cannot serve: it keeps no index of task names nor of failed tasks.
UNSUPPORTED_TOOLS = [
    "backend_registered_tasks",
    "backend_list_failed_tasks",
]


@pytest.mark.parametrize("name", UNSUPPORTED_TOOLS)
async def test_unsupported_tool_raises_unsupported_backend_tool(name, app):
    with pytest.raises(UnsupportedBackendTool, match=f"backend 'rq' does not support {name}$"):
        await app.tools.registered[name]()


def test_backend_class_is_registered(app):
    assert RqBackend in app.backends.registered
    assert tai42_backend_rq.RqBackend is RqBackend


def test_backend_extensions_are_registered(app):
    registered = dict(app.extensions.registered)
    for extension_name in ("sync_task", "schedule_task", "async_task"):
        assert registered[extension_name] is ExtensionKind.BACKEND
        assert callable(app.extensions.factories[extension_name])
