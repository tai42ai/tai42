"""The twelve task and worker tools the kit registers over a backend's worker inspection."""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.app import tai42_app

from tai42_kit.backend import INSPECTION_TOOLS, BackendInspection, UnsupportedBackendTool, register_inspection_tools
from tai42_kit.backend.job_context import running_job
from tai42_kit.utils.lc.signature_util import resolved_type_hints

from .surface_fakes import OWNER, SurfaceApp, SurfaceTools

#: Each tool with the arguments a call of it takes.
_CALLS: list[tuple[str, tuple[Any, ...]]] = [
    ("backend_ping_worker", ()),
    ("backend_list_active_workers", ()),
    ("backend_task_status", ("t1",)),
    ("backend_task_result", ("t1",)),
    ("backend_cancel_task", ("t1",)),
    ("backend_active_tasks", ()),
    ("backend_reserved_tasks", ()),
    ("backend_scheduled_tasks", ()),
    ("backend_registered_tasks", ()),
    ("backend_list_failed_tasks", ()),
    ("backend_worker_stats", ()),
    ("backend_worker_queues", ()),
]

#: The tools taking a ``worker_name``.
_ADDRESSED = [
    "backend_ping_worker",
    "backend_active_tasks",
    "backend_registered_tasks",
    "backend_worker_stats",
    "backend_worker_queues",
]


class _Inspection(BackendInspection):
    """Every view answered, each reply naming the view and what it was given."""

    def __init__(self, *, addresses_workers: bool = True, terminates: bool = True) -> None:
        super().__init__("fake")
        self._addresses_workers = addresses_workers
        self._terminates = terminates
        self.result: tuple[bool, str, Any] = (True, "done", "value")
        self.result_timeouts: list[float | None] = []

    @property
    def addresses_workers(self) -> bool:
        return self._addresses_workers

    @property
    def terminates(self) -> bool:
        return self._terminates

    async def ping(self, worker_name: str | None) -> dict[str, Any]:
        return {"ping": worker_name}

    async def active_workers(self) -> list[str]:
        return ["w1"]

    async def task_status(self, task_id: str) -> str:
        return f"status of {task_id}"

    async def task_result(self, task_id: str, timeout: float | None) -> tuple[bool, str, Any]:
        self.result_timeouts.append(timeout)
        return self.result

    async def cancel(self, task_id: str, terminate: bool) -> str:
        return f"cancel {task_id} terminate={terminate}"

    async def active_tasks(self, worker_name: str | None) -> dict[str, Any]:
        return {"active": worker_name}

    async def reserved_tasks(self) -> dict[str, Any]:
        return {"reserved": None}

    async def scheduled_tasks(self) -> dict[str, Any]:
        return {"scheduled": None}

    async def registered_tasks(self, worker_name: str | None) -> dict[str, Any]:
        return {"registered": worker_name}

    async def failed_tasks(self) -> list[dict[str, Any]]:
        return [{"failed": None}]

    async def worker_stats(self, worker_name: str | None) -> dict[str, Any]:
        return {"stats": worker_name}

    async def worker_queues(self, worker_name: str | None) -> dict[str, Any]:
        return {"queues": worker_name}


def _register(inspection: BackendInspection) -> SurfaceTools:
    app = SurfaceApp()
    with tai42_app.bound(app):
        register_inspection_tools(inspection, OWNER)
    return app.tools


def test_the_twelve_tools_are_registered_as_the_owning_backends_tagged_backend() -> None:
    tools = _register(BackendInspection("fake"))
    assert set(tools.registered) == set(INSPECTION_TOOLS)
    assert {name for name, _ in _CALLS} == set(INSPECTION_TOOLS)
    for name, registered in tools.registered.items():
        assert registered.tags == {"backend"}, name
        assert registered.module == OWNER, name
        resolved_type_hints(registered.func)


#: The reply type each tool declares: the MCP surface describes a tool's reply from it, so a
#: reply declared ``Any`` reaches a client as bare text with no structured content.
_REPLY_TYPES: dict[str, Any] = {
    "backend_ping_worker": dict[str, Any],
    "backend_list_active_workers": list[str],
    "backend_task_status": str,
    "backend_task_result": Any,
    "backend_cancel_task": str,
    "backend_active_tasks": dict[str, Any],
    "backend_reserved_tasks": dict[str, Any],
    "backend_scheduled_tasks": dict[str, Any],
    "backend_registered_tasks": dict[str, Any],
    "backend_list_failed_tasks": list[dict[str, Any]],
    "backend_worker_stats": dict[str, Any],
    "backend_worker_queues": dict[str, Any],
}


def test_each_tool_declares_the_structured_reply_its_view_returns() -> None:
    tools = _register(BackendInspection("fake"))
    assert set(_REPLY_TYPES) == set(INSPECTION_TOOLS)
    for name, registered in tools.registered.items():
        assert resolved_type_hints(registered.func)["return"] == _REPLY_TYPES[name], name


def test_the_base_inspection_declares_no_capability() -> None:
    inspection = BackendInspection("fake")
    assert inspection.label == "fake"
    assert inspection.addresses_workers is False
    assert inspection.terminates is False


@pytest.mark.parametrize(("name", "args"), _CALLS)
async def test_each_view_the_base_inspection_keeps_refuses_naming_its_tool(name: str, args: tuple[Any, ...]) -> None:
    tools = _register(BackendInspection("fake"))
    with pytest.raises(UnsupportedBackendTool, match=f"^backend 'fake' does not support {name}$") as refused:
        await tools[name](*args)
    assert (refused.value.label, refused.value.tool) == ("fake", name)


async def test_each_tool_replies_its_view() -> None:
    tools = _register(_Inspection())
    assert await tools["backend_ping_worker"]("w1") == {"ping": "w1"}
    assert await tools["backend_ping_worker"]() == {"ping": None}
    assert await tools["backend_list_active_workers"]() == ["w1"]
    assert await tools["backend_task_status"]("t1") == "status of t1"
    assert await tools["backend_task_result"]("t1") == "value"
    assert await tools["backend_cancel_task"]("t1") == "cancel t1 terminate=False"
    assert await tools["backend_cancel_task"]("t1", terminate=True) == "cancel t1 terminate=True"
    assert await tools["backend_active_tasks"]("w1") == {"active": "w1"}
    assert await tools["backend_reserved_tasks"]() == {"reserved": None}
    assert await tools["backend_scheduled_tasks"]() == {"scheduled": None}
    assert await tools["backend_registered_tasks"]("w1") == {"registered": "w1"}
    assert await tools["backend_list_failed_tasks"]() == [{"failed": None}]
    assert await tools["backend_worker_stats"]("w1") == {"stats": "w1"}
    assert await tools["backend_worker_queues"]("w1") == {"queues": "w1"}


@pytest.mark.parametrize("name", _ADDRESSED)
async def test_a_worker_name_is_refused_when_the_backend_cannot_address_workers(name: str) -> None:
    tools = _register(_Inspection(addresses_workers=False))
    assert list((await tools[name]()).values()) == [None]
    with pytest.raises(UnsupportedBackendTool, match=f"^backend 'fake' does not support {name} with a worker_name$"):
        await tools[name]("w1")


async def test_terminate_is_refused_when_the_backend_cannot_stop_a_running_task() -> None:
    tools = _register(_Inspection(terminates=False))
    assert await tools["backend_cancel_task"]("t1") == "cancel t1 terminate=False"
    with pytest.raises(
        UnsupportedBackendTool, match=r"^backend 'fake' does not support backend_cancel_task with terminate$"
    ):
        await tools["backend_cancel_task"]("t1", terminate=True)


async def test_an_unfinished_task_replies_the_not_ready_text() -> None:
    inspection = _Inspection()
    inspection.result = (False, "in_progress", None)
    tools = _register(inspection)
    assert await tools["backend_task_result"]("t1", 2.5) == "Task t1 is not ready (status: in_progress)"
    assert inspection.result_timeouts == [2.5]


async def test_a_job_reads_a_result_at_once_but_never_waits_for_one() -> None:
    inspection = _Inspection()
    tools = _register(inspection)
    with running_job():
        assert await tools["backend_task_result"]("t1") == "value"
        with pytest.raises(RuntimeError, match=r"^backend_task_result cannot wait"):
            await tools["backend_task_result"]("t1", 1.0)
    assert inspection.result_timeouts == [None]
