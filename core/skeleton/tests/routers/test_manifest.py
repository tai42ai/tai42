"""Manifest router: get-manifest, mcp-config write, mcp-status, mcp reload."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app

import tai42_skeleton.routers.manifest as router
from tai42_skeleton.app import instance
from tests._fakes.bus import FakeBus


def _req(body=None, **path_params) -> Request:
    async def _json():
        return body

    return cast(Request, SimpleNamespace(json=_json, path_params=path_params, query_params={}))


def _data(resp):
    return json.loads(bytes(resp.body))


class _FakeConfigManager:
    """The transactional config-manager seams the ConfigService pipeline drives.

    ``mutate_manifest`` runs the mutator on a copy and persists it only if the mutator
    returns without raising (a rejected mutation leaves the store untouched);
    ``replace_manifest`` swaps the whole document. ``written`` records the last
    persisted document (``None`` until a persist lands)."""

    def __init__(self, manifest, env=None):
        self._manifest = manifest
        self._env = dict(env or {})
        self.written = None

    def read_manifest(self):
        return deepcopy(self._manifest)

    def read_manifest_preserved(self):
        return deepcopy(self._manifest)

    def read_env(self):
        return dict(self._env)

    def write_env(self, config):
        # Merge (like the file provider), dropping empties.
        self._env = {k: v for k, v in {**self._env, **config}.items() if v not in (None, "")}

    def replace_env(self, config):
        self._env = {k: v for k, v in config.items() if v not in (None, "")}

    def mutate_manifest(self, mutator):
        document = deepcopy(self._manifest)
        mutator(document)  # a raise here propagates before any persist
        self._manifest = document
        self.written = document
        return document

    def replace_manifest(self, document):
        self._manifest = deepcopy(document)
        self.written = deepcopy(document)
        return deepcopy(document)


@pytest.fixture
def fake(monkeypatch):
    live = {"mcp": [{"title": "gh", "config": {"url": "https://x"}}], "user_tools": ["b", "a"]}
    cm = _FakeConfigManager({"mcp": [], "tools": []})
    admin = SimpleNamespace(
        live_manifest=live,
        live_mcp_status=lambda: {"bound": {"gh": ["t1"]}, "failed": []},
        reload_config=lambda: {"status": "ok", "env_keys": 3},
        reload_mcp=lambda title: {"title": title, "status": "ok", "tools": ["t1"]},
    )
    # No worker bus: reloads stay local-only (fan-out has its own test). Patch the
    # contract handle's impl so the router body and the fanout helper both resolve
    # ``tai42_app`` to this fake.
    fake_app = SimpleNamespace(
        admin=admin,
        config=SimpleNamespace(config_manager=cm),
        backends=SimpleNamespace(backend=None),
    )
    monkeypatch.setattr(tai42_app, "_impl", fake_app)
    bus = FakeBus(origin="serve-x")
    monkeypatch.setattr(instance.app, "_bus", bus)
    return SimpleNamespace(app=fake_app, cm=cm, live=live, bus=bus)


async def test_get_manifest_serves_preserved_markers_not_resolved_secrets(fake):
    # RETIGHTEN: GET /api/manifest now serves the PRESERVED persisted manifest (markers
    # intact), NOT the resolved live_manifest — a secret leaf is its `!ENV ${KEY}` marker,
    # never the plaintext value. The fake live_manifest carries a resolved secret; the
    # preserved store carries the marker. The response must reflect the store, not the live view.
    fake.cm._manifest = {
        "mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {"Authorization": "!ENV ${GITHUB_TOKEN}"}}}],
        "user_tools": ["b", "a"],
    }
    fake.live["mcp"] = [{"title": "gh", "config": {"headers": {"Authorization": "super-secret-plaintext"}}}]
    resp = await router.get_manifest(_req())
    body = _data(resp)["data"]
    assert body["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GITHUB_TOKEN}"  # marker, no leak
    assert "super-secret-plaintext" not in json.dumps(body)
    assert body["user_tools"] == ["a", "b"]  # sorted


async def test_get_manifest_preserved_route(fake):
    # The explicit /api/manifest/preserved door serves the same preserved {mcp, user_tools}
    # view with markers intact — the source the Studio McpTab editor reads.
    fake.cm._manifest = {
        "mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {"Authorization": "!ENV ${GITHUB_TOKEN}"}}}],
        "user_tools": ["search"],
    }
    resp = await router.get_manifest_preserved(_req())
    body = _data(resp)["data"]
    assert body["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GITHUB_TOKEN}"
    assert body["user_tools"] == ["search"]


async def test_secret_env_writes_marker_and_marked_secret_env_key_not_leaked(fake):
    # The combined door writes the secret VALUE to the env store (marked secret), writes an
    # `!ENV ${KEY}` MARKER at the pointer, and returns reloadConfigResult — the generated key
    # is NEVER in the response, and the two writes stay consistent.
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    resp = await router.set_mcp_secret_env(
        _req(
            {
                "value": "a-pasted-secret",
                "key_hint": "GITHUB_TOKEN",
                "manifest_pointer": "mcp/0/config/headers/Authorization",
            }
        )
    )
    assert resp.status_code == 200
    body = _data(resp)["data"]
    # reloadConfigResult shape (NOT profileApplyResponse): status + env_keys + fanout.
    assert body["status"] == "ok"
    assert "fanout" in body
    # The manifest carries the marker (no plaintext secret baked in).
    assert fake.cm._manifest["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GITHUB_TOKEN}"
    # The env store holds the secret value, marked secret via TAI_ENV_SECRET_KEYS.
    assert fake.cm._env["GITHUB_TOKEN"] == "a-pasted-secret"
    assert "GITHUB_TOKEN" in fake.cm._env["TAI_ENV_SECRET_KEYS"].split(",")
    # Neither the generated key NAME nor the secret VALUE ever rides the response.
    serialized = json.dumps(body)
    assert "GITHUB_TOKEN" not in serialized
    assert "a-pasted-secret" not in serialized


async def test_secret_env_non_mcp_head_is_400(fake):
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    resp = await router.set_mcp_secret_env(
        _req({"value": "s", "key_hint": "K", "manifest_pointer": "tools/0/env/API_KEY"})
    )
    assert resp.status_code == 400
    assert "mcp" in _data(resp)["error"]
    # Neither store was touched by a rejected pointer.
    assert fake.cm._env == {}


async def test_secret_env_out_of_range_pointer_is_400_and_writes_nothing(fake):
    # A pointer whose list index is out of range fails at candidate validation (before any
    # write), so it is a loud 400 and neither store is touched.
    fake.cm._manifest = {"mcp": []}
    resp = await router.set_mcp_secret_env(
        _req({"value": "s", "key_hint": "K", "manifest_pointer": "mcp/0/config/headers/Authorization"})
    )
    assert resp.status_code == 400
    assert fake.cm._env == {}
    assert fake.cm.written is None


async def test_secret_env_dangling_marker_elsewhere_is_400_naming_var(fake, monkeypatch):
    # The shared boundary validator fires on the secret-env door too: a manifest carrying a
    # pre-existing `!ENV ${VAR}` marker with no env var behind it is a loud 400 naming the var,
    # and nothing is written (the refusal is at candidate validation, before any store write).
    monkeypatch.delenv("PREEXISTING_MISSING", raising=False)
    fake.cm._manifest = {
        "mcp": [
            {
                "title": "gh",
                "config": {"url": "https://x", "headers": {"X-Other": "!ENV ${PREEXISTING_MISSING}"}},
            }
        ]
    }
    resp = await router.set_mcp_secret_env(
        _req(
            {
                "value": "s",
                "key_hint": "GITHUB_TOKEN",
                "manifest_pointer": "mcp/0/config/headers/Authorization",
            }
        )
    )
    assert resp.status_code == 400
    assert "PREEXISTING_MISSING" in _data(resp)["error"]
    assert fake.cm._env == {}


async def test_secret_env_explicit_key_collision_different_value_is_400(fake):
    # C9c: an EXPLICIT `key` colliding with an existing stored key holding a DIFFERENT value is
    # refused with a loud 400 naming the key — never a silent overwrite of a live secret.
    fake.cm._env = {"GITHUB_TOKEN": "the-live-secret"}
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    resp = await router.set_mcp_secret_env(
        _req(
            {
                "value": "a-DIFFERENT-secret",
                "key": "GITHUB_TOKEN",
                "manifest_pointer": "mcp/0/config/headers/Authorization",
            }
        )
    )
    assert resp.status_code == 400
    assert "GITHUB_TOKEN" in _data(resp)["error"]
    # Nothing written: the live secret is untouched and the manifest carries no marker.
    assert fake.cm._env == {"GITHUB_TOKEN": "the-live-secret"}
    assert fake.cm._manifest["mcp"][0]["config"]["headers"] == {}


async def test_secret_env_explicit_key_same_value_is_idempotent(fake):
    # An explicit key matching an existing stored VALUE is not a collision (idempotent re-send):
    # the op writes the marker and marks the key, the stored value unchanged.
    fake.cm._env = {"GITHUB_TOKEN": "same-secret"}
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    resp = await router.set_mcp_secret_env(
        _req(
            {
                "value": "same-secret",
                "key": "GITHUB_TOKEN",
                "manifest_pointer": "mcp/0/config/headers/Authorization",
            }
        )
    )
    assert resp.status_code == 200
    assert fake.cm._manifest["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GITHUB_TOKEN}"
    assert fake.cm._env["GITHUB_TOKEN"] == "same-secret"
    assert "GITHUB_TOKEN" in fake.cm._env["TAI_ENV_SECRET_KEYS"].split(",")


async def test_secret_env_explicit_invalid_key_is_400(fake):
    # An explicit key outside the shell-identifier charset is a loud 400 before any write.
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    resp = await router.set_mcp_secret_env(
        _req({"value": "s", "key": "bad-key!", "manifest_pointer": "mcp/0/config/headers/Authorization"})
    )
    assert resp.status_code == 400
    assert fake.cm._env == {}


async def test_secret_env_requires_exactly_one_of_key_or_hint(fake):
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    # Neither key nor key_hint → 400.
    neither = await router.set_mcp_secret_env(
        _req({"value": "s", "manifest_pointer": "mcp/0/config/headers/Authorization"})
    )
    assert neither.status_code == 400
    # Both key and key_hint → 400.
    both = await router.set_mcp_secret_env(
        _req(
            {
                "value": "s",
                "key": "K",
                "key_hint": "H",
                "manifest_pointer": "mcp/0/config/headers/Authorization",
            }
        )
    )
    assert both.status_code == 400
    assert fake.cm._env == {}


async def test_secret_env_generated_key_never_shadows_registered_env_var(fake):
    # C9c shadow-avoidance: even with the stored env FREE of the candidate, a generated key that
    # would match a REGISTERED settings env_var is skipped — the generator mints a fresh,
    # non-shadowing key (suffix). Uses a REAL registered, non-X-band var as the target so the op
    # must consult registered_env_var_names() (the X band alone would not carry it).
    from tai42_skeleton.config.boundary import registered_env_var_names, x_band_env_keys

    target = sorted(registered_env_var_names() - x_band_env_keys())[0]
    fake.cm._env = {}  # the stored env does NOT hold the target — only the registry does
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    resp = await router.set_mcp_secret_env(
        _req(
            {
                "value": "fresh-secret",
                "key_hint": target,  # base derives to exactly the registered var
                "manifest_pointer": "mcp/0/config/headers/Authorization",
            }
        )
    )
    assert resp.status_code == 200
    assert target not in fake.cm._env  # the registered var is NOT shadowed
    assert fake.cm._env[f"{target}_2"] == "fresh-secret"  # a fresh, non-shadowing key was minted
    assert fake.cm._manifest["mcp"][0]["config"]["headers"]["Authorization"] == f"!ENV ${{{target}_2}}"


async def test_secret_env_marks_appended_not_clobbered_reads_stored_env(fake):
    # C9d: the op APPENDS the new key to the marks read from the STORED env (never the settings
    # cache), so a pre-existing stored mark survives and the new key joins it.
    fake.cm._env = {"DB_SECRET": "v", "TAI_ENV_SECRET_KEYS": "DB_SECRET"}
    fake.cm._manifest = {"mcp": [{"title": "gh", "config": {"url": "https://x", "headers": {}}}]}
    resp = await router.set_mcp_secret_env(
        _req(
            {
                "value": "new-secret",
                "key_hint": "API_TOKEN",
                "manifest_pointer": "mcp/0/config/headers/Authorization",
            }
        )
    )
    assert resp.status_code == 200
    marks = fake.cm._env["TAI_ENV_SECRET_KEYS"].split(",")
    assert "DB_SECRET" in marks  # the pre-existing stored mark is NOT clobbered
    assert "API_TOKEN" in marks  # the new key is appended


async def test_mcp_config_schema_shape():
    # No app impl needed: the handler only calls a pydantic classmethod.
    resp = await router.get_mcp_config_schema(_req())
    data = _data(resp)["data"]
    assert isinstance(data, dict)
    # A JSON-Schema object for a model with a nested ``config`` sub-model.
    assert "properties" in data or "$ref" in data or "$defs" in data


async def test_mcp_config_schema_round_trip():
    from tai42_skeleton.manifest import Manifest

    # An entry shaped per the served schema's required fields (``title`` + a
    # ``config`` MCPConfig with exactly one transport) must pass full manifest
    # validation.
    entry = {"title": "x", "config": {"type": "streamable_http", "url": "https://example.com/mcp"}}
    manifest = Manifest.model_validate({"mcp": [entry], "tools": [{"title": "t", "module": "m"}]})
    assert manifest.mcp[0].title == "x"
    assert manifest.mcp[0].config.url == "https://example.com/mcp"


async def test_mcp_status(fake):
    resp = await router.get_mcp_status(_req())
    assert _data(resp)["data"]["bound"] == {"gh": ["t1"]}


async def test_mcp_reload_known(fake):
    resp = await router.reload_mcp(_req(title="gh"))
    data = _data(resp)["data"]
    # The response is the per-origin fleet report; this worker's re-probe result
    # rides its self-entry payload.
    assert data["op"] == "reload_mcp"
    assert data["results"][0]["payload"]["status"] == "ok"


async def test_mcp_reload_unknown_404(fake):
    resp = await router.reload_mcp(_req(title="nope"))
    assert resp.status_code == 404


async def test_mcp_config_missing_key_400(fake):
    resp = await router.set_mcp_config(_req({}))
    assert resp.status_code == 400


async def test_mcp_config_dangling_marker_is_400_naming_var(fake, monkeypatch):
    # The shared boundary validator fires on the EXISTING POST /api/mcp-config too: an mcp
    # entry that introduces an `!ENV ${VAR}` marker with no env var behind it is a loud 400
    # naming the var (it would otherwise silently resolve to "N/A").
    monkeypatch.delenv("MISSING_DANGLER", raising=False)
    resp = await router.set_mcp_config(
        _req(
            {
                "mcp": [
                    {
                        "title": "gh",
                        "config": {"url": "https://x", "headers": {"Authorization": "!ENV ${MISSING_DANGLER}"}},
                    }
                ]
            }
        )
    )
    assert resp.status_code == 400
    assert "MISSING_DANGLER" in _data(resp)["error"]
    assert fake.cm.written is None


async def test_mcp_config_invalid_400(fake):
    # A non-list mcp fails Manifest validation loudly.
    resp = await router.set_mcp_config(_req({"mcp": "not-a-list"}))
    assert resp.status_code == 400
    assert fake.cm.written is None


async def test_mcp_config_valid_persists_and_reloads(fake):
    resp = await router.set_mcp_config(_req({"mcp": []}))
    assert resp.status_code == 200
    assert _data(resp)["data"] == {
        "status": "ok",
        "env_keys": 3,
        "fanout": {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"},
    }
    assert fake.cm.written is not None
    assert fake.cm.written["mcp"] == []


# -- the new mcp-status / manifest routes (C4 domain work) -------------------


def _query_req(query: str = "", **path_params) -> Request:
    from starlette.datastructures import QueryParams

    return cast(
        Request,
        SimpleNamespace(json=None, path_params=path_params, query_params=QueryParams(query)),
    )


@pytest.fixture
def fake_full(monkeypatch):
    """A live app whose admin answers the whole mcp-status surface, no worker bus."""
    live = {"mcp": [{"title": "gh"}], "user_tools": []}
    cm = _FakeConfigManager({"mcp": [], "tools": []})

    admin = SimpleNamespace(
        live_manifest=live,
        list_failed_mcps=lambda: [{"title": "gh", "status": "unavailable"}],
        reload_failed_mcps=lambda: [{"title": "gh", "status": "ok"}],
        deregister_mcp=lambda title: {"title": title, "status": "ok", "removed": [f"{title}_t"]},
        reload_mcp=lambda title: {"title": title, "status": "ok"},
        reload_config=lambda: {"status": "ok", "env_keys": 0},
    )
    fake_app = SimpleNamespace(
        admin=admin,
        config=SimpleNamespace(config_manager=cm),
        backends=SimpleNamespace(backend=None),
    )
    monkeypatch.setattr(tai42_app, "_impl", fake_app)
    bus = FakeBus(origin="serve-x")
    monkeypatch.setattr(instance.app, "_bus", bus)
    return SimpleNamespace(app=fake_app, cm=cm, bus=bus)


async def test_update_manifest_valid_replaces(fake_full):
    resp = await router.update_manifest(_req({"manifest_text": "mcp: []\n"}))
    assert resp.status_code == 200
    data = _data(resp)["data"]
    # Persist-through: the whole posted document is persisted and reloaded, and the
    # response embeds the fleet report (local-only here — no worker bus configured).
    assert data["fanout"] == {
        "mode": "local-only",
        "note": "no worker bus configured; only this worker reloaded",
    }
    assert fake_full.cm.written == {"mcp": []}


async def test_update_manifest_invalid_body_400(fake_full):
    # A non-mapping manifest fails ManifestReplace validation at the HTTP edge → 400.
    resp = await router.update_manifest(_req({"manifest": "not-a-mapping"}))
    assert resp.status_code == 400
    assert "invalid manifest" in _data(resp)["error"]


async def test_update_manifest_non_object_body_400(fake_full):
    resp = await router.update_manifest(_req(["not", "an", "object"]))
    assert resp.status_code == 400
    assert _data(resp)["error"] == "request body must be a JSON object"


async def test_list_failed_mcps_route(fake_full):
    resp = await router.list_failed_mcps(_query_req(""))
    assert resp.status_code == 200
    assert _data(resp)["data"]["results"][0]["payload"] == [{"title": "gh", "status": "unavailable"}]


async def test_reload_failed_mcps_route(fake_full):
    resp = await router.reload_failed_mcps(_req({"targets": None}))
    assert resp.status_code == 200
    assert _data(resp)["data"]["results"][0]["payload"] == [{"title": "gh", "status": "ok"}]


async def test_reload_failed_mcps_route_no_body(fake_full):
    # A POST with no body → targets None → applied on this worker, its result on the
    # self entry of the fleet report.
    resp = await router.reload_failed_mcps(_req(None))
    assert resp.status_code == 200
    assert _data(resp)["data"]["results"][0]["payload"] == [{"title": "gh", "status": "ok"}]


async def test_deregister_mcp_route(fake_full):
    resp = await router.deregister_mcp(_req(None, title="gh"))
    assert resp.status_code == 200
    assert _data(resp)["data"]["results"][0]["payload"] == {"title": "gh", "status": "ok", "removed": ["gh_t"]}


async def test_mcp_config_malformed_json_400(fake):
    # A body whose JSON does not parse is a loud 400 via the HTTP-edge extractor.
    async def _raise():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    req = cast(Request, SimpleNamespace(json=_raise, path_params={}, query_params={}))
    resp = await router.set_mcp_config(req)
    assert resp.status_code == 400
    assert "Expecting value" in _data(resp)["error"]


async def test_reload_mcp_route_targeting_self(fake_full):
    # Targeting this worker by its origin → it re-probes locally and the report
    # carries its self entry.
    resp = await router.reload_mcp(_req({"targets": ["serve-x"]}, title="gh"))
    assert resp.status_code == 200
    data = _data(resp)["data"]
    assert data["results"][0]["origin"] == "serve-x"
    assert data["results"][0]["payload"] == {"title": "gh", "status": "ok"}
