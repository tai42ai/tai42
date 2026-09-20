"""The worker-tool surface reaches Celery's inspect API, and the Celery CLI
entrypoint invokes the umbrella command."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import tai42_backend_celery.core.backend as backend_module
import tai42_backend_celery.tools.tools as tools


def test_celery_cli_main_invokes_the_umbrella_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import celery.__main__ as celery_main_module

    calls: list[str] = []
    monkeypatch.setattr(celery_main_module, "main", lambda: calls.append("ran"))
    backend_module._celery_cli_main()
    assert calls == ["ran"]


# --- worker-tool surface over celery inspect ----------------------------------


class _FakeInspect:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.requested: list[Any] = []

    def _answer(self) -> Any:
        return self._payload

    ping = active = reserved = scheduled = registered = stats = active_queues = _answer


@pytest.fixture
def fake_inspect(monkeypatch: pytest.MonkeyPatch) -> _FakeInspect:
    inspect = _FakeInspect({"celery@a": {"ok": "pong"}})
    fake_app = SimpleNamespace(control=SimpleNamespace(inspect=lambda dest=None: inspect))
    monkeypatch.setattr(tools, "celery_app", fake_app)
    return inspect


def test_ping_and_list_active_workers(fake_inspect: _FakeInspect) -> None:
    assert tools.backend_ping_worker() == {"celery@a": {"ok": "pong"}}
    assert tools.backend_ping_worker("celery@a") == {"celery@a": {"ok": "pong"}}
    assert tools.backend_list_active_workers() == ["celery@a"]


def test_inspect_tools_return_empty_mappings_when_no_reply(fake_inspect: _FakeInspect) -> None:
    fake_inspect._payload = None
    assert tools.backend_ping_worker() == {}
    assert tools.backend_list_active_workers() == []
    assert tools.backend_active_tasks() == {}
    assert tools.backend_reserved_tasks() == {}
    assert tools.backend_scheduled_tasks() == {}
    assert tools.backend_registered_tasks() == {}
    assert tools.backend_worker_stats() == {}
    assert tools.backend_worker_queues() == {}


def test_inspect_tools_pass_replies_through(fake_inspect: _FakeInspect) -> None:
    fake_inspect._payload = {"celery@a": ["something"]}
    assert tools.backend_active_tasks("celery@a") == {"celery@a": ["something"]}
    assert tools.backend_reserved_tasks() == {"celery@a": ["something"]}
    assert tools.backend_scheduled_tasks() == {"celery@a": ["something"]}
    assert tools.backend_registered_tasks() == {"celery@a": ["something"]}
    assert tools.backend_worker_stats() == {"celery@a": ["something"]}
    assert tools.backend_worker_queues() == {"celery@a": ["something"]}


def test_task_status_and_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResult:
        status = "STARTED"

        def __init__(self) -> None:
            self.revoked: list[bool] = []

        def revoke(self, terminate: bool = False) -> None:
            self.revoked.append(terminate)

    fake = _FakeResult()
    monkeypatch.setattr(tools, "AsyncResult", lambda task_id, app=None: fake)
    assert tools.backend_task_status("t1") == "STARTED"
    assert tools.backend_cancel_task("t1", terminate=True) == "Task t1 revoked (terminate=True)"
    assert fake.revoked == [True]
