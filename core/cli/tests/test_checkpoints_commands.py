"""``tai checkpoints`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_checkpoints_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/checkpoints/sweep"
        return data_response({"deleted": 3})

    assert run_cli(monkeypatch, handler, ["checkpoints", "sweep"]).exit_code == 0
