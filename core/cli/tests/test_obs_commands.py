"""``tai obs`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_obs_metrics_passes_params(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/observability/metrics"
        assert request.url.params.get("from") == "7d"
        assert request.url.params.get("granularity") == "day"
        return data_response({"summary": {}})

    result = run_cli(monkeypatch, handler, ["obs", "metrics", "--from", "7d", "--granularity", "day"])
    assert result.exit_code == 0, result.output


def test_obs_metrics_passes_to_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/observability/metrics"
        assert request.url.params.get("to") == "now"
        return data_response({"summary": {"runs": 3}})

    result = run_cli(monkeypatch, handler, ["obs", "metrics", "--to", "now"])
    assert result.exit_code == 0, result.output
