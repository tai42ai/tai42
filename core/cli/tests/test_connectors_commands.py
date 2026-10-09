"""``tai connectors`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_connectors_connect_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections/start"
        body = json.loads(request.content)
        assert body["provider_id"] == "google"
        assert body["enabled_sub_services"] == ["gmail"]
        return data_response({"flow_id": "f1", "authorize_url": "https://x"})

    result = run_cli(
        monkeypatch, handler, ["connectors", "connect", "google", "--alias", "work", "--sub-service", "gmail"]
    )
    assert result.exit_code == 0, result.output


def test_connectors_get_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("connection not found", 404)

    result = run_cli(monkeypatch, handler, ["connectors", "get", "missing"])
    assert result.exit_code != 0
    assert "connection not found" in result.output


def test_connectors_providers_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/providers"
        return data_response(
            {
                "providers": [{"id": "google", "display_name": "Google", "kind": "oauth", "category": "email"}],
                "categories": [{"id": "email", "display_name": "Email", "sort_order": 1}],
            }
        )

    result = run_cli(monkeypatch, handler, ["connectors", "providers"])
    assert result.exit_code == 0, result.output
    assert "google" in result.output


def test_connectors_connections_uses_items_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections"
        return data_response(
            {"items": [{"connection_id": "c1", "provider_id": "google", "alias": "work", "auth_health_state": "ok"}]}
        )

    result = run_cli(monkeypatch, handler, ["connectors", "connections"])
    assert result.exit_code == 0, result.output
    assert "c1" in result.output


def test_connectors_get_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections/c1"
        return data_response({"connection_id": "c1", "alias": "work"})

    result = run_cli(monkeypatch, handler, ["connectors", "get", "c1"])
    assert result.exit_code == 0, result.output
    assert "work" in result.output


def test_connectors_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/connectors/connections/c1"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["connectors", "disconnect", "c1"])
    assert result.exit_code == 0, result.output


def test_connectors_reconnect_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/connectors/connections/c1/reconnect"
        assert json.loads(request.content) == {"enabled_sub_services": ["gmail"], "return_url": "/connectors"}
        return data_response({"authorize_url": "https://x"})

    result = run_cli(monkeypatch, handler, ["connectors", "reconnect", "c1", "--sub-service", "gmail"])
    assert result.exit_code == 0, result.output


def test_connectors_sub_services_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/connectors/connections/c1/sub-services"
        assert json.loads(request.content) == {
            "enabled_sub_services": ["gmail", "calendar"],
            "return_url": "/connectors",
        }
        return data_response({"enabled_sub_services": ["gmail", "calendar"]})

    result = run_cli(
        monkeypatch,
        handler,
        ["connectors", "sub-services", "c1", "--sub-service", "gmail", "--sub-service", "calendar"],
    )
    assert result.exit_code == 0, result.output


def _capture_connect(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections/start"
        seen.update(json.loads(request.content))
        return data_response({"authorize_url": "https://x"})

    return handler


def test_connect_reads_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_file = tmp_path / "c.json"
    config_file.write_text('{"api_key":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_connect(seen),
        ["connectors", "connect", "prov", "--alias", "work", "--sub-service", "svc", "--config-file", str(config_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["config_values"] == {"api_key": "secret"}


def test_connect_reads_config_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_connect(seen),
        ["connectors", "connect", "prov", "--alias", "work", "--sub-service", "svc", "--config-file", "-"],
        stdin='{"api_key":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["config_values"] == {"api_key": "secret"}


def test_connect_rejects_both_config_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_file = tmp_path / "c.json"
    config_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_connect({}),
        [
            "connectors",
            "connect",
            "prov",
            "--alias",
            "work",
            "--sub-service",
            "svc",
            "--config",
            "{}",
            "--config-file",
            str(config_file),
        ],
    )
    assert result.exit_code != 0
    assert "--config-file" in result.output


# -- the provider door -------------------------------------------------------------------------

_SECRET = "cli-client-secret-value"

_VIEW = {
    "url": "https://mcp.example.com/mcp",
    "resource": "https://mcp.example.com/mcp",
    "resource_name": None,
    "issuer": "https://auth.example.com",
    "authorization_servers": ["https://auth.example.com", "https://other.example.com"],
    "client_issuer": None,
    "registration_supported": False,
    "automatic_client": False,
    "scopes_supported": ["files"],
    "suggested_id": "mcp_example_com",
    "suggested_display_name": "mcp.example.com",
    "redirect_uri": "http://testserver/oauth-bridge.html",
}

_ENTRY = {
    "id": "mcp_example_com",
    "display_name": "mcp.example.com",
    "kind": "oauth",
    "origin": "community",
    "category": "other",
    "sub_services": [{"id": "default", "display_name": "mcp.example.com", "scopes": []}],
    "removable": True,
    "client_source": "unset",
}


def _capture(seen: dict, path: str, method: str, answer: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        assert request.url.path == path
        seen["body"] = json.loads(request.content) if request.content else None
        return data_response(answer)

    return handler


def test_discover_by_address_prints_every_sign_in_service_and_the_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    handler = _capture(seen, "/api/connectors/providers/discover", "POST", _VIEW)
    result = run_cli(monkeypatch, handler, ["connectors", "discover", "https://mcp.example.com/mcp"])
    assert result.exit_code == 0, result.output
    assert seen["body"] == {"url": "https://mcp.example.com/mcp"}
    assert "https://other.example.com" in result.output
    assert "http://testserver/oauth-bridge.html" in result.output


def test_discover_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    handler = _capture(seen, "/api/connectors/providers/discover", "POST", {**_VIEW, "redirect_uri": None})
    result = run_cli(monkeypatch, handler, ["connectors", "discover", "--provider", "remote"])
    assert result.exit_code == 0, result.output
    assert seen["body"] == {"provider_id": "remote"}
    assert "no sign-in can start from this address" in result.output


@pytest.mark.parametrize("args", [[], ["https://mcp.example.com/mcp", "--provider", "remote"]])
def test_discover_needs_exactly_one_of_an_address_and_a_provider(monkeypatch: pytest.MonkeyPatch, args) -> None:
    result = run_cli(monkeypatch, _capture({}, "/x", "POST", {}), ["connectors", "discover", *args])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_add_server_body_and_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    handler = _capture(seen, "/api/connectors/providers", "POST", {"provider": _ENTRY, "fanout": None})
    result = run_cli(
        monkeypatch,
        handler,
        [
            "connectors",
            "add-server",
            "https://mcp.example.com/mcp",
            "--id",
            "team_tools",
            "--name",
            "Team tools",
            "--description",
            "Shared",
            "--icon-url",
            "https://icons.example.com/t.png",
            "--category",
            "data",
            "--scope",
            "files.read",
            "--scope",
            "files.write",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"] == {
        "url": "https://mcp.example.com/mcp",
        "id": "team_tools",
        "display_name": "Team tools",
        "description": "Shared",
        "icon_url": "https://icons.example.com/t.png",
        "category": "data",
        "scopes": ["files.read", "files.write"],
    }
    assert "tai connectors connect mcp_example_com --alias" in result.output
    assert "--sub-service default" in result.output


def test_add_server_sends_only_what_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    handler = _capture(seen, "/api/connectors/providers", "POST", {"provider": _ENTRY, "fanout": None})
    result = run_cli(monkeypatch, handler, ["connectors", "add-server", "https://mcp.example.com/mcp"])
    assert result.exit_code == 0, result.output
    assert seen["body"] == {"url": "https://mcp.example.com/mcp"}


def test_remove_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    answer = {"provider_id": "remote", "removed_clients": 1, "fanout": None}
    handler = _capture(seen, "/api/connectors/providers/remote", "DELETE", answer)
    result = run_cli(monkeypatch, handler, ["connectors", "remove-server", "remote"])
    assert result.exit_code == 0, result.output
    assert "removed_clients" in result.output


def test_set_client_reads_the_secret_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    handler = _capture(seen, "/api/connectors/providers/remote/client", "PUT", {"provider": _ENTRY, "fanout": None})
    result = run_cli(
        monkeypatch,
        handler,
        [
            "connectors",
            "set-client",
            "remote",
            "--client-id",
            "app",
            "--client-secret-file",
            "-",
            "--auth-method",
            "client_secret_basic",
            "--issuer",
            "https://auth.example.com",
        ],
        stdin=f"{_SECRET}\n",
    )
    assert result.exit_code == 0, result.output
    assert seen["body"] == {
        "client_id": "app",
        "client_secret": _SECRET,
        "token_endpoint_auth_method": "client_secret_basic",
        "issuer": "https://auth.example.com",
    }
    assert _SECRET not in result.output


def test_set_client_reads_the_secret_from_a_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(f"{_SECRET}\n")
    seen: dict = {}
    handler = _capture(seen, "/api/connectors/providers/remote/client", "PUT", {"provider": _ENTRY, "fanout": None})
    result = run_cli(
        monkeypatch,
        handler,
        ["connectors", "set-client", "remote", "--client-id", "app", "--client-secret-file", str(secret_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"] == {"client_id": "app", "client_secret": _SECRET}


def test_set_client_without_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    handler = _capture(seen, "/api/connectors/providers/remote/client", "PUT", {"provider": _ENTRY, "fanout": None})
    result = run_cli(monkeypatch, handler, ["connectors", "set-client", "remote", "--client-id", "app"])
    assert result.exit_code == 0, result.output
    assert seen["body"] == {"client_id": "app"}


def test_set_client_refuses_the_secret_as_an_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch,
        _capture({}, "/x", "PUT", {}),
        ["connectors", "set-client", "remote", "--client-id", "app", "--client-secret", _SECRET],
    )
    assert result.exit_code != 0
    assert "No such option" in result.output


def test_set_client_refuses_an_empty_secret_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("\n")
    result = run_cli(
        monkeypatch,
        _capture({}, "/x", "PUT", {}),
        ["connectors", "set-client", "remote", "--client-id", "app", "--client-secret-file", str(secret_file)],
    )
    assert result.exit_code != 0
    assert "--client-secret-file" in result.output


def test_providers_show_removable_and_client_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"providers": [_ENTRY], "categories": []})

    result = run_cli(monkeypatch, handler, ["connectors", "providers"])
    assert result.exit_code == 0, result.output
    assert "removable" in result.output
    assert "client_source" in result.output
    assert "unset" in result.output


def test_connections_show_the_scopes_a_connection_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(
            {
                "items": [
                    {"connection_id": "c1", "provider_id": "p", "alias": "a", "scopes_wanted": ["files.write", "x"]},
                    {"connection_id": "c2", "provider_id": "p", "alias": "b", "scopes_wanted": []},
                ],
                "total": 2,
                "unhealthy": 0,
            }
        )

    result = run_cli(monkeypatch, handler, ["connectors", "connections"])
    assert result.exit_code == 0, result.output
    assert "needs: files.write, x" in result.output
    assert result.output.count("needs:") == 1
    as_json = run_cli(monkeypatch, handler, ["connectors", "connections"], json_output=True)
    assert json.loads(as_json.output)["items"][0]["scopes_wanted"] == ["files.write", "x"]
