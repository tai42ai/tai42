"""End-to-end checks that list commands render their MODEL-DERIVED columns.

Since the CLI's table columns and list-envelope key are now derived from each
route's response model (via ``commands._route_columns``), these drive a few
representative commands against a fake server and assert the visible table reflects
the derived shape — a bare-list scalar wrap, an enveloped list, and a full
row-model column set — rather than a hand-written column list.
"""

from __future__ import annotations

import httpx

from .remote_harness import data_response, run_cli


def _serves(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(payload)

    return handler


def test_tools_list_renders_the_scalar_value_column(monkeypatch) -> None:
    # GET /api/tools is a bare list of names -> a single derived ``value`` column.
    result = run_cli(monkeypatch, _serves(["echo", "weather"]), ["tools", "list"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].strip() == "value"
    assert "echo" in result.output
    assert "weather" in result.output


def test_notifications_list_derives_the_envelope_items_key(monkeypatch) -> None:
    # NotificationListing{notifications: [...]}: the derived items_key pulls the rows.
    payload = {"notifications": [{"id": "n1", "message": "hi", "recipient": "alice"}]}
    result = run_cli(monkeypatch, _serves(payload), ["notifications", "list"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header[:2] == ["id", "message"]
    assert "n1" in result.output


def test_roles_list_derives_the_full_row_model_columns(monkeypatch) -> None:
    # RoleDefinitionList (a bare list of RoleDefinition) -> the row model's fields, so
    # columns the old hand-written list omitted (condition*, scopes) now appear.
    payload = [
        {
            "condition": None,
            "condition_id": None,
            "condition_kwargs": {},
            "name": "ops",
            "description": "operators",
            "scopes": ["hooks:read"],
            "base_tier": "member",
            "allow_all": False,
            "grants": {},
        }
    ]
    result = run_cli(monkeypatch, _serves(payload), ["roles", "list"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header[0] == "condition"
    assert "scopes" in header
    assert "ops" in result.output


def test_connectors_connections_list_derives_columns(monkeypatch) -> None:
    payload = {"items": [{"connection_id": "c1", "provider_id": "github", "kind": "oauth"}]}
    result = run_cli(monkeypatch, _serves(payload), ["connectors", "connections"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].split()[0] == "connection_id"
    assert "github" in result.output


def test_reencrypt_tokens_renders_the_sweep_summary(monkeypatch) -> None:
    # The reencrypt route has a generated table entry, but the command renders the
    # whole summary (counts + failed ids) as a single result.
    payload = {
        "scanned": 3,
        "reencrypted": 2,
        "skipped": 1,
        "failed": 0,
        "failed_connection_ids": [],
        "cas_retries": 0,
    }
    result = run_cli(monkeypatch, _serves(payload), ["connectors", "reencrypt-tokens"])
    assert result.exit_code == 0, result.output
    assert "scanned" in result.output
    assert "reencrypted" in result.output
