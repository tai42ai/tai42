"""``tai runs`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_runs_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/runs"
        return data_response(
            {
                "items": [
                    {
                        "runId": "run_1",
                        "preset": "assistant",
                        "outcome": "parked",
                        "interactionId": "i_42",
                        "resumedInteractions": ["i_7"],
                    }
                ],
                "page": 1,
                "nextPage": None,
            }
        )

    result = run_cli(monkeypatch, handler, ["runs", "list"])
    assert result.exit_code == 0, result.output
    assert "run_1" in result.output
    assert "i_42" in result.output  # the lifecycle-correlation column is listed
    assert "i_7" in result.output  # the resumed-interaction column is listed


def test_runs_list_passes_all_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/runs"
        params = request.url.params
        assert params.get("preset") == "assistant"
        assert params.get("version") == "3"
        assert params.get("user") == "u1"
        assert params.get("session") == "s1"
        assert params.get("interaction") == "i1"
        assert params.get("outcome") == "aborted"
        assert params.get("from") == "2026-01-01T00:00:00"
        assert params.get("to") == "2026-02-01T00:00:00"
        assert params.get("page") == "2"
        assert params.get("pageSize") == "25"
        return data_response({"items": [{"runId": "run_9", "outcome": "aborted"}], "page": 2, "nextPage": None})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "runs",
            "list",
            "--preset",
            "assistant",
            "--version",
            "3",
            "--user",
            "u1",
            "--session",
            "s1",
            "--interaction",
            "i1",
            "--outcome",
            "aborted",
            "--from",
            "2026-01-01T00:00:00",
            "--to",
            "2026-02-01T00:00:00",
            "--page",
            "2",
            "--page-size",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "run_9" in result.output


def test_runs_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/runs/prune"
        return data_response({"retention_days": 30, "pruned_count": 4})

    result = run_cli(monkeypatch, handler, ["runs", "prune"])
    assert result.exit_code == 0, result.output
    assert "4" in result.output
