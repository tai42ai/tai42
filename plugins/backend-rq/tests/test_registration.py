"""Import-time registration: the canonical tool surface, the three BACKEND
extensions, and the backend class itself."""

from __future__ import annotations

import pytest
from tai42_contract.extensions import ExtensionKind

import tai42_backend_rq
from tai42_backend_rq import extensions as extensions_module
from tai42_backend_rq import tools as tools_module
from tai42_backend_rq.backend import RqBackend

# Canonical task/worker tools every backend exposes.
TASK_WORKER_TOOLS = [
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
]

# Canonical schedule tools every backend exposes.
SCHEDULE_TOOLS = [
    "backend_schedule_exists",
    "backend_get_schedule",
    "backend_list_schedules",
    "backend_delete_schedule",
    "backend_enable_schedule",
    "backend_disable_schedule",
    "backend_run_schedule_now",
    "backend_update_schedule",
    "backend_export_schedules",
    "backend_import_schedules",
]

# Canonical tools the RQ backend does not implement; these raise loudly.
NOT_IMPLEMENTED_TOOLS = [
    "backend_registered_tasks",
    "backend_list_failed_tasks",
]


@pytest.mark.parametrize("name", TASK_WORKER_TOOLS + SCHEDULE_TOOLS)
def test_canonical_tool_is_registered(name, app):
    assert callable(getattr(tools_module, name))
    assert name in app.tools.registered


@pytest.mark.parametrize("name", TASK_WORKER_TOOLS + SCHEDULE_TOOLS)
def test_canonical_tool_is_tagged_backend(name, app):
    assert app.tools.tags[name] == {"backend"}


@pytest.mark.parametrize("name", NOT_IMPLEMENTED_TOOLS)
async def test_unsupported_tool_raises_not_implemented(name):
    stub = getattr(tools_module, name)
    with pytest.raises(NotImplementedError) as excinfo:
        await stub()
    assert f"backend 'rq' does not support {name}" in str(excinfo.value)


def test_backend_class_is_registered(app):
    assert RqBackend in app.backends.registered
    assert tai42_backend_rq.RqBackend is RqBackend


def test_backend_extensions_are_registered(app):
    registered = dict(app.extensions.registered)
    for extension_name in ("sync_task", "schedule_task", "async_task"):
        assert registered[extension_name] is ExtensionKind.BACKEND
        assert callable(getattr(extensions_module, extension_name))
