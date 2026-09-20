"""Import-time registration: the canonical tool surface, the three BACKEND-kind
extensions, the backend class, and the shutdown hook."""

from __future__ import annotations

import inspect

import pytest
from tai42_contract.extensions import ExtensionKind

from tai42_backend_arq import extensions, lifecycle, tools
from tai42_backend_arq.backend import ArqBackend

CANONICAL_TOOLS = {
    # Task / worker
    "backend_task_status",
    "backend_task_result",
    "backend_cancel_task",
    "backend_active_tasks",
    "backend_reserved_tasks",
    "backend_scheduled_tasks",
    "backend_registered_tasks",
    "backend_worker_stats",
    "backend_worker_queues",
    "backend_ping_worker",
    "backend_list_active_workers",
    "backend_list_failed_tasks",
    # Schedule
    "backend_schedule_exists",
    "backend_get_schedule",
    "backend_list_schedules",
    "backend_export_schedules",
    "backend_import_schedules",
    "backend_delete_schedule",
    "backend_enable_schedule",
    "backend_disable_schedule",
    "backend_run_schedule_now",
    "backend_update_schedule",
}

# Capabilities the arq broker has no reliable data model for: registered as
# tools but raise loudly.
NOT_IMPLEMENTED_TOOLS = {
    "backend_registered_tasks",
    "backend_worker_stats",
    "backend_worker_queues",
    "backend_ping_worker",
    "backend_list_active_workers",
}


def test_canonical_tools_registered(stub_app) -> None:
    registered = set(stub_app.tools.registered)
    assert registered == CANONICAL_TOOLS, (
        f"missing: {CANONICAL_TOOLS - registered}; extra: {registered - CANONICAL_TOOLS}"
    )


def test_canonical_tools_tagged_backend(stub_app) -> None:
    for name in CANONICAL_TOOLS:
        assert stub_app.tools.tags[name] == {"backend"}


@pytest.mark.parametrize("name", sorted(NOT_IMPLEMENTED_TOOLS))
async def test_not_implemented_stubs_raise(name: str) -> None:
    func = getattr(tools, name)
    assert inspect.iscoroutinefunction(func)
    with pytest.raises(NotImplementedError) as exc_info:
        await func()
    assert str(exc_info.value) == f"backend 'arq' does not support {name}"


def test_backend_extensions_registered(stub_app) -> None:
    registered = stub_app.extensions.registered
    for name in ("sync_task", "schedule_task", "async_task"):
        kind, factory = registered[name]
        assert kind is ExtensionKind.BACKEND
        assert factory is getattr(extensions, name)


def test_backend_class_registered(stub_app) -> None:
    assert stub_app.backends.registered_cls is ArqBackend
    assert isinstance(stub_app.backends.instance, ArqBackend)


def test_shutdown_hook_registered(stub_app) -> None:
    assert lifecycle.close_arq_pool in stub_app.lifecycle.shutdown_handlers


async def test_shutdown_hook_closes_pool(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from tai42_backend_arq.pool import RedisPoolManager

    close = AsyncMock()
    monkeypatch.setattr(RedisPoolManager, "close", close)
    await lifecycle.close_arq_pool()
    close.assert_awaited_once()
