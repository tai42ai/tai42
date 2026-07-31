"""Fork-safety hooks: child init evicts the monitoring client, child shutdown
flushes spans and closes task-loop clients."""

from __future__ import annotations

import asyncio
import logging

import pytest

import tai42_backend_celery.core.app as app_module
from tai42_backend_celery.core.tasks import callback_task, tool_execution


def test_worker_process_init_evicts_monitoring_client(stub_app, monkeypatch, caplog) -> None:
    monkeypatch.setattr(app_module.sys, "platform", "linux")
    with caplog.at_level(logging.INFO):
        app_module.on_worker_process_init(sender="worker-1")
    stub_app.monitoring.active.writer.shutdown.assert_called_once_with()
    assert any("fork-safe shutdown" in r.message for r in caplog.records)
    assert not any("macOS" in r.message for r in caplog.records)


def test_worker_process_init_names_the_darwin_hazard(stub_app, monkeypatch, caplog) -> None:
    monkeypatch.setattr(app_module.sys, "platform", "darwin")
    with caplog.at_level(logging.WARNING):
        app_module.on_worker_process_init(sender="worker-1")
    stub_app.monitoring.active.writer.shutdown.assert_called_once_with()
    assert any("macOS" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_worker_process_shutdown_flushes_and_closes_loops(stub_app) -> None:
    tool_execution._loop = asyncio.new_event_loop()
    callback_task._loop = asyncio.new_event_loop()
    app_module.on_worker_process_shutdown(sender="worker-1")
    stub_app.monitoring.active.writer.flush.assert_called_once_with()
    assert tool_execution._loop is None
    assert callback_task._loop is None
    assert stub_app.clients.shutdown_calls == 2


def test_worker_process_shutdown_flush_failure_is_logged_not_raised(stub_app, caplog) -> None:
    stub_app.monitoring.active.writer.flush.side_effect = RuntimeError("exporter gone")
    with caplog.at_level(logging.WARNING):
        app_module.on_worker_process_shutdown(sender="worker-1")
    assert any("Error flushing monitoring" in r.message for r in caplog.records)


def test_worker_process_shutdown_loop_close_failure_is_logged_not_raised(stub_app, monkeypatch, caplog) -> None:
    def _boom() -> None:
        raise RuntimeError("loop wedged")

    monkeypatch.setattr(tool_execution, "close_loop", _boom)
    with caplog.at_level(logging.WARNING):
        app_module.on_worker_process_shutdown(sender="worker-1")
    assert any("Error closing task loop clients" in r.message for r in caplog.records)


@pytest.fixture(autouse=True)
def _reset_task_loops():
    yield
    for task in (tool_execution, callback_task):
        loop = getattr(task, "_loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        task._loop = None
