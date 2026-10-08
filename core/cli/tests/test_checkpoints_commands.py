"""``tai checkpoints`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli

_SWEEP_RESULT = {
    "provider": "postgres",
    "waiting_minutes": 10080,
    "finished_minutes": 1440,
    "swept_count": 2,
    "finished_swept": ["t-done"],
    "waiting_swept": ["t-idle"],
    "spared": ["t-parked"],
    "skipped": None,
}


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert request.url.path == "/api/checkpoints/sweep"
    return data_response(_SWEEP_RESULT)


def test_checkpoints_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _handler, ["checkpoints", "sweep"])
    assert result.exit_code == 0
    for value in ("t-done", "t-idle", "t-parked", "postgres"):
        assert value in result.output


def test_checkpoints_sweep_json(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _handler, ["checkpoints", "sweep"], json_output=True)
    assert result.exit_code == 0
    assert json.loads(result.output) == _SWEEP_RESULT
