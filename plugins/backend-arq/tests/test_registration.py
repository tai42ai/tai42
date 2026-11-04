"""Import-time registration: the three BACKEND extensions, the backend class itself, and the
views arq cannot answer refusing loudly."""

from __future__ import annotations

import pytest
from tai42_contract.extensions import ExtensionKind
from tai42_kit.backend import UnsupportedBackendTool

from tai42_backend_arq.backend import ArqBackend

from .conftest import StubApp

# The tools arq cannot serve: its workers leave no per-worker record of their names, task
# names, stats or queues.
UNSUPPORTED_TOOLS = [
    "backend_registered_tasks",
    "backend_worker_stats",
    "backend_worker_queues",
    "backend_ping_worker",
    "backend_list_active_workers",
]


@pytest.mark.parametrize("name", UNSUPPORTED_TOOLS)
async def test_unsupported_tool_raises_unsupported_backend_tool(name: str, stub_app: StubApp) -> None:
    with pytest.raises(UnsupportedBackendTool, match=f"backend 'arq' does not support {name}$"):
        await stub_app.tools.registered[name]()


def test_backend_extensions_registered(stub_app: StubApp) -> None:
    registered = stub_app.extensions.registered
    for name in ("sync_task", "schedule_task", "async_task"):
        kind, factory = registered[name]
        assert kind is ExtensionKind.BACKEND
        assert callable(factory)


def test_backend_class_registered(stub_app: StubApp) -> None:
    assert stub_app.backends.registered_cls is ArqBackend
    assert isinstance(stub_app.backends.instance, ArqBackend)
