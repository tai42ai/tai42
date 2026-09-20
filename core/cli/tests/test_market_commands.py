"""``tai plugins`` commands against a fake server.

Each command's request shaping (path, query params, JSON body), the envelope
rendering, and the typed-error surfacing are exercised through the shared remote
harness (an httpx ``MockTransport``), so no request leaves the process.
"""

from __future__ import annotations

import json

import httpx

from .remote_harness import data_response, error_response, run_cli, strip_ansi


def _capture():
    """A handler that records the last request and answers a canned data envelope;
    returns ``(handler, captured)`` where ``captured`` is filled per call."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        captured["body"] = json.loads(request.content) if request.content else None
        return data_response(captured.get("_payload", {"listings": []}))

    return handler, captured


# -- search ------------------------------------------------------------------


def test_search_positional_becomes_q_with_facets(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["plugins", "search", "uuid", "--kind", "tool"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/search"
    params = httpx.QueryParams(captured["query"])
    assert params.get("q") == "uuid"
    assert params.get("kind") == "tool"


def test_search_no_positional_sends_no_q(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["plugins", "search", "--tier", "official"])
    assert result.exit_code == 0, result.output
    params = httpx.QueryParams(captured["query"])
    assert "q" not in params
    assert params.get("tier") == "official"


def test_search_repeated_tag_becomes_repeated_tags_param(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["plugins", "search", "--tag", "a", "--tag", "b"])
    assert result.exit_code == 0, result.output
    # Never comma-joined: each --tag is its own repeated tags query param.
    assert captured["query"].count("tags=") == 2
    assert httpx.QueryParams(captured["query"]).get_list("tags") == ["a", "b"]


# -- info --------------------------------------------------------------------


def test_info_splits_ref_into_the_path(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {"ref": "tai42/toolbox"}
    result = run_cli(monkeypatch, handler, ["plugins", "info", "tai42/toolbox"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/plugins/tai42/toolbox"


def test_info_malformed_ref_is_a_usage_error(monkeypatch) -> None:
    handler, _ = _capture()
    result = run_cli(monkeypatch, handler, ["plugins", "info", "noslash"])
    assert result.exit_code != 0
    assert "namespace/name" in result.output


# -- categories / installed --------------------------------------------------


def test_categories_issues_the_get(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = ["dev", "data"]
    result = run_cli(monkeypatch, handler, ["plugins", "categories"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/categories"


def test_kinds_issues_the_get(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = ["tool", "agent"]
    result = run_cli(monkeypatch, handler, ["plugins", "kinds"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/kinds"


def test_installed_json_passthrough(monkeypatch) -> None:
    handler, captured = _capture()
    payload = {
        "installed": [
            {
                "ref": "tai42/toolbox",
                "version": "1.0.0",
                "latest": "2.0.0",
                "update_available": True,
                "incompatible_newer": None,
                "compat": {"status": "compatible", "reason": None},
            }
        ],
        "quarantined": [{"name": "acme_plugin", "reason": "tools module failed to import: boom"}],
    }
    captured["_payload"] = payload
    result = run_cli(monkeypatch, handler, ["plugins", "installed"], json_output=True)
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/installed"
    assert json.loads(result.output) == payload


def test_installed_table_shows_compat_status_and_quarantine(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {
        "installed": [
            {
                "ref": "tai42/toolbox",
                "version": "1.0.0",
                "latest": None,
                "update_available": False,
                "incompatible_newer": "2.0.0",
                "compat": {"status": "incompatible", "reason": "needs a newer core"},
            }
        ],
        "quarantined": [{"name": "acme_plugin", "reason": "tools module failed to import: boom"}],
    }
    result = run_cli(monkeypatch, handler, ["plugins", "installed"])
    assert result.exit_code == 0, result.output
    # The compat STATUS lands in its own table column, and a quarantined plugin
    # is printed after the table — never invisible in the default view.
    assert "incompatible" in result.output
    assert "quarantined: acme_plugin — tools module failed to import: boom" in result.output


# -- install / uninstall / update bodies -------------------------------------


def test_install_posts_ref_and_version(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {"ref": "tai42/toolbox"}
    result = run_cli(monkeypatch, handler, ["plugins", "install", "tai42/toolbox", "--version", "1.2.3"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/marketplace/install"
    assert captured["body"] == {"ref": "tai42/toolbox", "version": "1.2.3"}


def test_install_mount_and_accept_public_routes_in_body(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {"ref": "acme/relay"}
    result = run_cli(
        monkeypatch,
        handler,
        ["plugins", "install", "acme/relay", "--mount", "relay=channels/relay-2", "--accept-public-routes"],
    )
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/install"
    assert captured["body"] == {
        "ref": "acme/relay",
        "route_mounts": {"relay": "channels/relay-2"},
        "accept_public_routes": True,
    }


def test_install_duplicate_mount_item_is_a_usage_error(monkeypatch) -> None:
    handler, _ = _capture()
    result = run_cli(
        monkeypatch, handler, ["plugins", "install", "acme/relay", "--mount", "relay=a", "--mount", "relay=b"]
    )
    assert result.exit_code != 0
    assert "more than once" in result.output


def test_install_dry_run_previews_and_does_not_install(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {
        "ref": "acme/relay",
        "version": "1.0.0",
        "items": [
            {
                "item": "relay",
                "base": "relay",
                "routes": [{"path": "/status", "full_path": "/api/relay/status", "methods": ["GET"], "public": True}],
            }
        ],
        "collisions": [],
        "new_public_routes": [{"item": "relay", "full_path": "/api/relay/status", "methods": ["GET"]}],
        "requires_public_acceptance": True,
    }
    result = run_cli(monkeypatch, handler, ["plugins", "install", "acme/relay", "--dry-run"])
    assert result.exit_code == 0, result.output
    # A dry-run hits ONLY the preview door, never the install door.
    assert captured["path"] == "/api/marketplace/install/preview"
    assert captured["body"] == {"ref": "acme/relay"}
    assert "/api/relay/status" in result.output


def test_install_dry_run_exits_nonzero_on_collision(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {
        "ref": "acme/relay",
        "version": "1.0.0",
        "items": [],
        "collisions": [
            {
                "item": "relay",
                "full_path": "/api/relay/status",
                "methods": ["GET"],
                "conflict_owner": "core",
                "conflict_path": "/api/relay/status",
            }
        ],
        "new_public_routes": [],
        "requires_public_acceptance": False,
    }
    result = run_cli(monkeypatch, handler, ["plugins", "install", "acme/relay", "--dry-run"])
    assert result.exit_code == 1
    assert "collision" in result.output


def _route_capture(preview_payload: dict, action_payload: dict):
    """A handler that routes ``/install/preview`` to ``preview_payload`` and any
    other POST to ``action_payload``; returns ``(handler, calls)`` recording every
    request so a test can assert both the preview and the mutating post."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path.endswith("/install/preview"):
            return data_response(preview_payload)
        return data_response(action_payload)

    return handler, calls


def _last(calls: list[dict], suffix: str) -> dict | None:
    """The last recorded call whose path ends with ``suffix`` (never the preview)."""
    hits = [c for c in calls if c["path"].endswith(suffix) and not c["path"].endswith("/install/preview")]
    return hits[-1] if hits else None


def test_install_env_and_secret_land_in_body(monkeypatch) -> None:
    handler, calls = _route_capture({"missing_env": [], "delivery": "descriptor"}, {"ref": "acme/iota"})
    result = run_cli(
        monkeypatch,
        handler,
        ["plugins", "install", "acme/iota", "--env", "IOTA_TOKEN=abc", "--secret", "IOTA_EXTRA"],
    )
    assert result.exit_code == 0, result.output
    install = _last(calls, "/install")
    assert install is not None
    assert install["body"] == {"ref": "acme/iota", "env": {"IOTA_TOKEN": "abc"}, "secret_keys": ["IOTA_EXTRA"]}


def test_install_uncovered_required_env_exits_nonzero_and_names_it(monkeypatch) -> None:
    handler, calls = _route_capture({"missing_env": ["IOTA_TOKEN", "KAPPA_KEY"]}, {"ref": "acme/iota"})
    result = run_cli(monkeypatch, handler, ["plugins", "install", "acme/iota", "--env", "IOTA_TOKEN=abc"])
    assert result.exit_code != 0
    # The still-missing var is named; the supplied one is not re-reported.
    assert "KAPPA_KEY" in result.output
    # The install is refused BEFORE it is posted — only the preview ran.
    assert _last(calls, "/install") is None


def test_install_supplied_required_env_proceeds(monkeypatch) -> None:
    handler, calls = _route_capture({"missing_env": ["IOTA_TOKEN"]}, {"ref": "acme/iota"})
    result = run_cli(monkeypatch, handler, ["plugins", "install", "acme/iota", "--env", "IOTA_TOKEN=abc"])
    assert result.exit_code == 0, result.output
    install = _last(calls, "/install")
    assert install is not None
    assert install["body"]["env"] == {"IOTA_TOKEN": "abc"}


def test_install_duplicate_env_key_is_a_usage_error(monkeypatch) -> None:
    handler, _ = _capture()
    result = run_cli(monkeypatch, handler, ["plugins", "install", "acme/iota", "--env", "K=a", "--env", "K=b"])
    assert result.exit_code != 0
    assert "more than once" in strip_ansi(result.output)


def test_install_dry_run_shows_delivery_and_required_env(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {
        "ref": "acme/iota",
        "delivery": "descriptor",
        "required_env": [{"name": "IOTA_TOKEN", "secret": True}],
        "missing_env": ["IOTA_TOKEN"],
        "items": [],
        "collisions": [],
    }
    result = run_cli(monkeypatch, handler, ["plugins", "install", "acme/iota", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/install/preview"
    out = strip_ansi(result.output)
    assert "delivery: descriptor" in out
    assert "IOTA_TOKEN" in out
    assert "secret" in out


def test_update_env_and_secret_land_in_body(monkeypatch) -> None:
    handler, calls = _route_capture({"missing_env": []}, {"ref": "acme/iota"})
    result = run_cli(
        monkeypatch,
        handler,
        ["plugins", "update", "acme/iota", "--env", "IOTA_TOKEN=abc", "--secret", "IOTA_EXTRA"],
    )
    assert result.exit_code == 0, result.output
    update = _last(calls, "/update")
    assert update is not None
    assert update["body"] == {"ref": "acme/iota", "env": {"IOTA_TOKEN": "abc"}, "secret_keys": ["IOTA_EXTRA"]}


def test_update_dry_run_previews_only(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {"delivery": "descriptor", "required_env": [], "items": [], "collisions": []}
    result = run_cli(monkeypatch, handler, ["plugins", "update", "acme/iota", "--dry-run"])
    assert result.exit_code == 0, result.output
    # A dry-run update hits only the shared preview door, never the update door.
    assert captured["path"] == "/api/marketplace/install/preview"
    assert "delivery: descriptor" in strip_ansi(result.output)


def test_update_uncovered_required_env_exits_nonzero(monkeypatch) -> None:
    handler, calls = _route_capture({"missing_env": ["KAPPA_KEY"]}, {"ref": "acme/iota"})
    result = run_cli(monkeypatch, handler, ["plugins", "update", "acme/iota"])
    assert result.exit_code != 0
    assert "KAPPA_KEY" in result.output
    assert _last(calls, "/update") is None


def test_uninstall_posts_ref(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {"ref": "tai42/toolbox", "uninstalled": True}
    result = run_cli(monkeypatch, handler, ["plugins", "uninstall", "tai42/toolbox"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/uninstall"
    assert captured["body"] == {"ref": "tai42/toolbox"}


def test_update_posts_ref_without_version_when_omitted(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {"ref": "tai42/toolbox"}
    result = run_cli(monkeypatch, handler, ["plugins", "update", "tai42/toolbox"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/update"
    assert captured["body"] == {"ref": "tai42/toolbox"}


# -- upgrade --all -------------------------------------------------------------


def test_upgrade_all_posts_and_renders_the_report(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {
        "results": [
            {"ref": "tai42/a", "outcome": "upgraded", "detail": "1.0.0 -> 1.2.0"},
            {"ref": "tai42/b", "outcome": "no-compatible-version", "detail": "no published version supports 0.3.0"},
        ]
    }
    result = run_cli(monkeypatch, handler, ["plugins", "upgrade", "--all"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/marketplace/upgrade-all"
    assert "upgraded" in result.output
    assert "no-compatible-version" in result.output


def test_upgrade_without_all_is_a_usage_error(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["plugins", "upgrade"])
    assert result.exit_code != 0
    # Rich styles the option token, splitting the dashes under forced colour;
    # assert on the de-styled visible text.
    assert "--all" in strip_ansi(result.output)
    assert "path" not in captured  # refused before any request


# -- error surfacing ---------------------------------------------------------


def test_conflict_error_envelope_surfaces_nonzero(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("tai42/toolbox is already installed", 409)

    result = run_cli(monkeypatch, handler, ["plugins", "install", "tai42/toolbox"])
    assert result.exit_code != 0
    assert "already installed" in result.output


def test_advisories_issues_the_get(monkeypatch) -> None:
    handler, captured = _capture()
    captured["_payload"] = {"advisories": [], "fetched_at": "2026-06-01T00:00:00+00:00"}
    result = run_cli(monkeypatch, handler, ["plugins", "advisories"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/marketplace/advisories"
