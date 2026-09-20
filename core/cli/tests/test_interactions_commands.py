"""The ``tai interactions`` remote command group, exercised against a fake
``/api/*`` server. The cancel command is the focus: its request shaping (a bodyless
POST to the per-interaction cancel door), its happy-path render, and a typed error
(conflict/not-found) surfacing as a non-zero exit carrying the server message.
"""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli, visible


def test_interactions_cancel_posts_to_the_cancel_door(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/interactions/i_123/cancel"
        assert request.headers["x-api-key"] == "test-key"
        # A bodyless request — the interaction id is the whole request.
        assert request.content == b""
        return data_response({"interaction_id": "i_123", "status": "cancelled"})

    result = run_cli(monkeypatch, handler, ["interactions", "cancel", "i_123"])
    assert result.exit_code == 0, result.output
    assert "cancelled" in result.output


def test_interactions_cancel_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"interaction_id": "i_123", "status": "cancelled"})

    result = run_cli(monkeypatch, handler, ["interactions", "cancel", "i_123"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"interaction_id": "i_123", "status": "cancelled"}


def test_interactions_cancel_conflict_surfaces_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("Interaction already answered", 409)

    result = run_cli(monkeypatch, handler, ["interactions", "cancel", "i_123"])
    assert result.exit_code != 0
    assert "Interaction already answered" in visible(result.output)
