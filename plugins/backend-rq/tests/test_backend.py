"""RqBackend: the ``launch`` dispatch to worker/beat/dashboard."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from tai42_backend_rq import worker as worker_module
from tai42_backend_rq.backend import RqBackend

# The registration decorator's static type is a union (decorator-or-class);
# at runtime the stub app returns the class unchanged.
_RqBackendCls: Any = RqBackend


@pytest.fixture
def backend() -> Any:
    return _RqBackendCls()


async def test_launch_requires_a_command(backend):
    with pytest.raises(ValueError, match="requires a command"):
        await backend.launch([])


async def test_launch_unknown_command_raises(backend):
    with pytest.raises(ValueError, match="Unknown RQ backend command 'flower'"):
        await backend.launch(["flower"])


async def test_launch_worker_parses_args(backend, monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_run(**params: Any) -> None:
        calls.append(params)

    monkeypatch.setattr(worker_module, "run_rq_worker", fake_run)

    await backend.launch(["worker", "--pool", "solo", "-n", "w1", "--burst"])

    assert calls == [
        {
            "redis_url": None,
            "name": "w1",
            "loglevel": "INFO",
            "burst": True,
            "results_ttl": 500,
            "pool": "solo",
        }
    ]


async def test_launch_worker_rejects_unknown_options(backend, monkeypatch):
    import click

    async def fake_run(**params: Any) -> None:
        pass

    monkeypatch.setattr(worker_module, "run_rq_worker", fake_run)
    with pytest.raises(click.UsageError):
        await backend.launch(["worker", "--frobnicate"])


async def test_launch_beat_runs_scheduler_main(backend, monkeypatch):
    import rq_scheduler.scripts.rqscheduler as rqscheduler_script

    seen_argv: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(rqscheduler_script, "main", lambda: seen_argv.append(list(sys.argv)))

    await backend.launch(["beat", "--interval", "5"])
    assert seen_argv == [["rq-scheduler", "--interval", "5"]]


async def test_launch_dashboard_runs_dashboard_cli(backend, monkeypatch):
    import rq_dashboard.cli as dashboard_cli

    seen_argv: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(dashboard_cli, "run", lambda: seen_argv.append(list(sys.argv)))

    await backend.launch(["dashboard", "--port", "9181"])
    assert seen_argv == [["rq-dashboard", "--port", "9181"]]
