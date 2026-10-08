"""Config router: env read, env write + reload, the active config mode, and the
settings-schema surface with per-field current-value overlay."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_kit.settings import SettingsClassInfo, SettingsFieldInfo, TaiBaseSettings

from tai42_skeleton.app import instance
from tai42_skeleton.operations import config as config_ops
from tai42_skeleton.routers import config as router
from tai42_skeleton.settings.env_secret_marks import env_secret_marks_settings

from .._fakes.bus import FakeBus


class _SecretDemoSettings(TaiBaseSettings):
    """Registered at import time — carries a ``SecretStr`` field so the schema
    route can be checked to report the field as secret AND round-trip its real
    value (the wire is unmasked)."""

    demo_secret: SecretStr | None = None


def _field(
    name: str,
    env_var: str,
    *,
    default: object = None,
    type_: str = "string",
    nested_group: str | None = None,
    default_namespace_var: str | None = None,
) -> SettingsFieldInfo:
    return SettingsFieldInfo(
        name=name,
        env_var=env_var,
        type=type_,
        default=default,
        required=False,
        secret=False,
        description=None,
        default_namespace_var=default_namespace_var,
        nested_group=nested_group,
        accepted_env_vars=[env_var] if env_var else [],
    )


@pytest.fixture(autouse=True)
def _clear_marks_cache():
    # The secret-marks accessor is an ``@settings_cache`` singleton keyed off the
    # process env; clear it around each test so ``TAI_ENV_SECRET_KEYS`` set by
    # one test never bleeds into another.
    env_secret_marks_settings.cache_clear()
    yield
    env_secret_marks_settings.cache_clear()


def _req() -> Request:
    return cast(Request, SimpleNamespace(path_params={}))


def _body_req(body: bytes) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/api/config/env", "headers": [], "query_string": b""}
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


class _FakeConfigManager:
    def __init__(self, env):
        self._env = env
        self.written: list[dict] = []

    def read_env(self):
        return self._env

    def read_manifest_preserved(self):
        # No backend registered, so the env-change invariant has nothing to reject.
        return {}

    def write_env(self, config):
        self.written.append(config)
        self._env = {**self._env, **config}


class _FakeAdmin:
    def __init__(self, manager, live_manifest=None):
        self._manager = manager
        self.reloads = 0
        self._live_manifest = live_manifest if live_manifest is not None else {}

    def reload_config(self):
        self.reloads += 1
        return {"status": "ok", "env_keys": len(self._manager._env)}

    @property
    def live_manifest(self):
        # The dumped live manifest the derived secret-marks read consults for
        # ``connectors[*].client_secret_env``; empty unless a test sets one.
        return self._live_manifest


@pytest.fixture
def install(monkeypatch):
    def _install(env=None, live_manifest=None):
        manager = _FakeConfigManager(env if env is not None else {})
        admin = _FakeAdmin(manager, live_manifest=live_manifest)
        # No worker bus: the reload stays local-only (the fan-out itself is
        # covered by the dedicated propagation test).
        impl = SimpleNamespace(
            config=SimpleNamespace(config_manager=manager),
            admin=admin,
            backends=SimpleNamespace(backend=None),
        )
        monkeypatch.setattr(tai42_app, "_impl", impl)
        bus = FakeBus(origin="serve-x")
        monkeypatch.setattr(instance.app, "_bus", bus)
        return SimpleNamespace(manager=manager, admin=admin, bus=bus)

    return _install


# -- GET /api/config/env -----------------------------------------------------


async def test_read_env(install, monkeypatch):
    install({"API_KEY": "abc", "DEBUG": "1"})
    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", "API_KEY")
    env_secret_marks_settings.cache_clear()
    resp = await router.read_env(_req())
    assert resp.status_code == 200
    assert _json(resp) == {"data": {"env": {"API_KEY": "abc", "DEBUG": "1"}, "secret_keys": ["API_KEY"]}}


async def test_read_env_derives_connector_secret_key_from_live_manifest(install, monkeypatch):
    # A live oauth connector's ``client_secret_env`` is DERIVED into the masked secret_keys
    # beyond the operator's own marks — so the connector's client secret shows as masked even
    # with no operator mark for it.
    install(
        {"API_KEY": "abc"},
        live_manifest={"connectors": [{"id": "acme", "kind": "oauth", "client_secret_env": "ACME_CLIENT_SECRET"}]},
    )
    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", "API_KEY")
    env_secret_marks_settings.cache_clear()
    resp = await router.read_env(_req())
    assert resp.status_code == 200
    # The operator mark AND the connector-derived key both appear (sorted, deduped).
    assert _json(resp)["data"]["secret_keys"] == ["ACME_CLIENT_SECRET", "API_KEY"]


async def test_read_env_missing_file_yields_empty_env(monkeypatch):
    class _Missing:
        def read_env(self):
            raise FileNotFoundError

    admin = SimpleNamespace(live_manifest={})
    impl = SimpleNamespace(config=SimpleNamespace(config_manager=_Missing()), admin=admin)
    monkeypatch.setattr(tai42_app, "_impl", impl)
    monkeypatch.delenv("TAI_ENV_SECRET_KEYS", raising=False)
    resp = await router.read_env(_req())
    assert resp.status_code == 200
    assert _json(resp) == {"data": {"env": {}, "secret_keys": []}}


# -- GET /api/config/settings-schema -----------------------------------------


async def test_settings_schema_shape(install, monkeypatch):
    install({})
    info = SettingsClassInfo(
        name="Demo",
        module="mod",
        qualname="mod.Demo",
        env_prefix="",
        env_prefix_owned=False,
        fields=[_field("a", "A_VAR", default="x")],
    )
    monkeypatch.setattr(config_ops, "registered_settings", lambda: [info])
    monkeypatch.delenv("A_VAR", raising=False)
    resp = await router.read_settings_schema(_req())
    assert resp.status_code == 200
    data = _json(resp)["data"]
    assert list(data.keys()) == ["groups"]
    group = data["groups"][0]
    assert group["name"] == "Demo"
    assert group["module"] == "mod"
    assert group["qualname"] == "mod.Demo"
    field = group["fields"][0]
    for key in (
        "name",
        "env_var",
        "type",
        "default",
        "required",
        "secret",
        "description",
        "nested_group",
        "value",
        "value_source",
    ):
        assert key in field
    assert field["value"] == "x"
    assert field["value_source"] == "field_default"


async def test_settings_schema_value_overlay(install, monkeypatch):
    # PROC_WINS is in BOTH the store and process env — process must win.
    install({"STORE_ONLY": "from_store", "PROC_WINS": "store_value"})
    monkeypatch.setenv("PROC_WINS", "proc_value")
    monkeypatch.delenv("STORE_ONLY", raising=False)
    monkeypatch.delenv("DEFAULT_ONLY", raising=False)
    info = SettingsClassInfo(
        name="Demo",
        module="mod",
        qualname="mod.Demo",
        env_prefix="",
        env_prefix_owned=False,
        fields=[
            _field("proc", "PROC_WINS"),
            _field("store", "STORE_ONLY"),
            _field("dflt", "DEFAULT_ONLY", default="the_default"),
            _field("nested", "", type_="object", nested_group="Other"),
        ],
    )
    monkeypatch.setattr(config_ops, "registered_settings", lambda: [info])
    resp = await router.read_settings_schema(_req())
    fields = {f["name"]: f for f in _json(resp)["data"]["groups"][0]["fields"]}
    assert fields["proc"]["value"] == "proc_value"  # process env wins over store
    assert fields["store"]["value"] == "from_store"  # store-only
    assert fields["dflt"]["value"] == "the_default"  # neither -> default
    assert fields["nested"]["value"] is None  # nested reference: non-editable


async def test_settings_schema_resolves_through_tai_default_namespace(install, monkeypatch):
    # A field whose own var and stored override are absent but which participates
    # in the shared TAI_DEFAULT_* namespace shows the value resolved through that layer
    # (process env, then store) BEFORE the bare field default — the server truth an
    # operator reads.
    install({"STORE_DEFAULT": "from_store_default"})
    monkeypatch.delenv("OWN_VAR", raising=False)
    monkeypatch.delenv("PROC_DEFAULT", raising=False)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://proc-default")
    info = SettingsClassInfo(
        name="Demo",
        module="mod",
        qualname="mod.Demo",
        env_prefix="",
        env_prefix_owned=False,
        fields=[
            _field("proc_dflt", "OWN_VAR", default="d", default_namespace_var="TAI_DEFAULT_REDIS_URL"),
            _field("store_dflt", "OTHER_VAR", default="d", default_namespace_var="STORE_DEFAULT"),
        ],
    )
    monkeypatch.setattr(config_ops, "registered_settings", lambda: [info])
    resp = await router.read_settings_schema(_req())
    fields = {f["name"]: f for f in _json(resp)["data"]["groups"][0]["fields"]}
    # Own var + stored override absent → resolve through TAI_DEFAULT_* (process wins).
    assert fields["proc_dflt"]["value"] == "redis://proc-default"
    # The default-namespace value can also come from the stored env layer.
    assert fields["store_dflt"]["value"] == "from_store_default"


async def test_settings_schema_empty_exported_var_reports_the_next_layer(install, monkeypatch):
    # The settings layer reads an empty variable as absent, so an exported empty value
    # reports the value and source of the next layer, as the field actually resolves.
    install({"EMPTY_PROC": "from_store", "EMPTY_DEFAULT_OWN": "", "STORE_EMPTY": ""})
    monkeypatch.setenv("EMPTY_PROC", "")
    monkeypatch.setenv("EMPTY_DEFAULT", "")
    monkeypatch.delenv("STORE_EMPTY", raising=False)
    monkeypatch.delenv("EMPTY_DEFAULT_OWN", raising=False)
    info = SettingsClassInfo(
        name="Demo",
        module="mod",
        qualname="mod.Demo",
        env_prefix="",
        env_prefix_owned=False,
        fields=[
            _field("proc", "EMPTY_PROC", default="d"),
            _field("stored", "STORE_EMPTY", default="d"),
            _field("dflt", "EMPTY_DEFAULT_OWN", default="d", default_namespace_var="EMPTY_DEFAULT"),
        ],
    )
    monkeypatch.setattr(config_ops, "registered_settings", lambda: [info])
    resp = await router.read_settings_schema(_req())
    fields = {f["name"]: f for f in _json(resp)["data"]["groups"][0]["fields"]}
    assert (fields["proc"]["value"], fields["proc"]["value_source"]) == ("from_store", "stored")
    assert (fields["stored"]["value"], fields["stored"]["value_source"]) == ("d", "field_default")
    assert (fields["dflt"]["value"], fields["dflt"]["value_source"]) == ("d", "field_default")


async def test_settings_schema_value_source_marks_provenance(install, monkeypatch):
    # Each field names which layer supplied its resolved value so a UI can badge provenance
    # (notably "from default" for a TAI_DEFAULT_* fallback, distinct from an explicit set).
    install({"STORE_ONLY": "from_store", "STORE_DEFAULT": "from_store_default"})
    monkeypatch.setenv("PROC_WINS", "proc_value")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://proc-default")
    monkeypatch.delenv("STORE_ONLY", raising=False)
    monkeypatch.delenv("DEFAULT_ONLY", raising=False)
    monkeypatch.delenv("OWN_VAR", raising=False)
    monkeypatch.delenv("OTHER_VAR", raising=False)
    info = SettingsClassInfo(
        name="Demo",
        module="mod",
        qualname="mod.Demo",
        env_prefix="",
        env_prefix_owned=False,
        fields=[
            _field("proc", "PROC_WINS"),
            _field("store", "STORE_ONLY"),
            _field("proc_dflt", "OWN_VAR", default="d", default_namespace_var="TAI_DEFAULT_REDIS_URL"),
            _field("store_dflt", "OTHER_VAR", default="d", default_namespace_var="STORE_DEFAULT"),
            _field("dflt", "DEFAULT_ONLY", default="the_default"),
            _field("nested", "", type_="object", nested_group="Other"),
        ],
    )
    monkeypatch.setattr(config_ops, "registered_settings", lambda: [info])
    resp = await router.read_settings_schema(_req())
    fields = {f["name"]: f for f in _json(resp)["data"]["groups"][0]["fields"]}
    assert fields["proc"]["value_source"] == "env"
    assert fields["store"]["value_source"] == "stored"
    # Both TAI_DEFAULT_* fallbacks (process-env and store-side) are one "from default" source.
    assert fields["proc_dflt"]["value_source"] == "default_namespace"
    assert fields["store_dflt"]["value_source"] == "default_namespace"
    assert fields["dflt"]["value_source"] == "field_default"
    assert fields["nested"]["value_source"] is None


async def test_settings_schema_secret_value_unmasked(install, monkeypatch):
    # Drive the REAL registry so the registered secret-bearing class appears.
    install({})
    monkeypatch.setenv("DEMO_SECRET", "supersecret")
    resp = await router.read_settings_schema(_req())
    groups = {g["name"]: g for g in _json(resp)["data"]["groups"]}
    assert "_SecretDemoSettings" in groups
    field = next(f for f in groups["_SecretDemoSettings"]["fields"] if f["name"] == "demo_secret")
    assert field["secret"] is True
    # The wire carries the REAL value — a display-side mask must NOT reach here.
    assert field["value"] == "supersecret"


async def test_settings_schema_missing_env_is_empty_not_500(monkeypatch):
    class _Missing:
        def read_env(self):
            raise FileNotFoundError

    impl = SimpleNamespace(config=SimpleNamespace(config_manager=_Missing()), admin=None)
    monkeypatch.setattr(tai42_app, "_impl", impl)
    monkeypatch.delenv("DEFAULT_X", raising=False)
    info = SettingsClassInfo(
        name="Demo",
        module="mod",
        qualname="mod.Demo",
        env_prefix="",
        env_prefix_owned=False,
        fields=[_field("dflt", "DEFAULT_X", default="d")],
    )
    monkeypatch.setattr(config_ops, "registered_settings", lambda: [info])
    resp = await router.read_settings_schema(_req())
    assert resp.status_code == 200  # missing .env -> empty overrides, not a 500
    fields = {f["name"]: f for f in _json(resp)["data"]["groups"][0]["fields"]}
    assert fields["dflt"]["value"] == "d"


async def test_secret_marks_roundtrip_and_group(install, monkeypatch):
    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", "API_KEY, DB_URL ,")
    env_secret_marks_settings.cache_clear()
    assert env_secret_marks_settings().secret_keys == ["API_KEY", "DB_URL"]
    install({})
    resp = await router.read_settings_schema(_req())
    names = [g["name"] for g in _json(resp)["data"]["groups"]]
    assert "EnvSecretMarksSettings" in names


# -- POST /api/config/env ----------------------------------------------------


async def test_write_env_happy(install):
    ctx = install({"OLD": "keep"})
    resp = await router.write_env(_body_req(b'{"env": {"NEW": "val"}}'))
    assert resp.status_code == 200
    assert _json(resp) == {
        "data": {
            "status": "ok",
            "env_keys": 2,
            "fanout": {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"},
        }
    }
    assert ctx.manager.written == [{"NEW": "val"}]
    assert ctx.admin.reloads == 1


async def test_write_env_non_string_value_400(install):
    ctx = install({})
    resp = await router.write_env(_body_req(b'{"env": {"PORT": 8080}}'))
    assert resp.status_code == 400
    assert "strings" in _json(resp)["error"]
    assert ctx.manager.written == []
    assert ctx.admin.reloads == 0


async def test_write_env_not_object_400(install):
    install({})
    resp = await router.write_env(_body_req(b'["a", "b"]'))
    assert resp.status_code == 400


async def test_write_env_bad_json_400(install):
    install({})
    resp = await router.write_env(_body_req(b"nope"))
    assert resp.status_code == 400
    assert "invalid JSON" in _json(resp)["error"]


async def test_write_env_manager_value_error_maps_to_400(install):
    """A malformed key rejected by the config manager (``ValueError``) becomes a
    400, not an uncaught 500."""
    ctx = install({})

    def _raise(config):
        raise ValueError("invalid env key 'BAD KEY': must match [A-Za-z_][A-Za-z0-9_]*")

    ctx.manager.write_env = _raise
    resp = await router.write_env(_body_req(b'{"env": {"BAD KEY": "val"}}'))
    assert resp.status_code == 400
    assert "invalid env key" in _json(resp)["error"]
    assert ctx.admin.reloads == 0


_ENV_BODY_SHAPE = 'the env write body is {"env": {name: value}, "secret_keys": [name] | null}'


async def test_write_env_secret_keys_replace_the_stored_marks(install):
    ctx = install({"TAI_ENV_SECRET_KEYS": "OLD_MARK", "API_KEY": "v"})
    resp = await router.write_env(_body_req(b'{"env": {"NEW": "x"}, "secret_keys": ["API_KEY", "NEW"]}'))
    assert resp.status_code == 200
    # One write carrying the env and the whole replaced marks set.
    assert ctx.manager.written == [{"NEW": "x", "TAI_ENV_SECRET_KEYS": "API_KEY,NEW"}]


async def test_write_env_without_secret_keys_leaves_the_marks(install):
    ctx = install({"TAI_ENV_SECRET_KEYS": "OLD_MARK"})
    for body in (b'{"env": {"NEW": "x"}}', b'{"env": {"NEW": "x"}, "secret_keys": null}'):
        resp = await router.write_env(_body_req(body))
        assert resp.status_code == 200
    assert ctx.manager.written == [{"NEW": "x"}, {"NEW": "x"}]


async def test_write_env_empty_secret_keys_deletes_the_marks(install):
    ctx = install({"TAI_ENV_SECRET_KEYS": "OLD_MARK"})
    resp = await router.write_env(_body_req(b'{"env": {}, "secret_keys": []}'))
    assert resp.status_code == 200
    assert ctx.manager.written == [{"TAI_ENV_SECRET_KEYS": ""}]


async def test_write_env_raw_marks_key_in_env_400(install):
    ctx = install({})
    resp = await router.write_env(_body_req(b'{"env": {"TAI_ENV_SECRET_KEYS": "A"}}'))
    assert resp.status_code == 400
    assert _json(resp)["error"] == "set secret marks through the 'secret_keys' field, not as an env key"
    assert ctx.manager.written == []


@pytest.mark.parametrize("body", [b'{"FOO": "x"}', b"{}", b'{"env": {}, "extra": 1}', b'{"env": "x"}'])
async def test_write_env_body_shape_400(install, body):
    ctx = install({})
    resp = await router.write_env(_body_req(body))
    assert resp.status_code == 400
    assert _json(resp)["error"] == _ENV_BODY_SHAPE
    assert ctx.manager.written == []


async def test_write_env_bad_mark_400(install):
    ctx = install({})
    resp = await router.write_env(_body_req(b'{"env": {}, "secret_keys": ["OK", "BAD KEY"]}'))
    assert resp.status_code == 400
    assert _json(resp)["error"] == "secret_keys entry 'BAD KEY' is not a valid env key"
    assert ctx.manager.written == []


@pytest.mark.parametrize("marks", [b'"A"', b"[1]"])
async def test_write_env_secret_keys_not_a_list_of_strings_400(install, marks):
    install({})
    resp = await router.write_env(_body_req(b'{"env": {}, "secret_keys": ' + marks + b"}"))
    assert resp.status_code == 400
    assert "secret_keys" in _json(resp)["error"]


async def test_write_env_unknown_owned_setting_400(install):
    ctx = install({})
    resp = await router.write_env(_body_req(b'{"env": {"INTERACTIONS_TYPO": "x"}}'))
    assert resp.status_code == 400
    assert "INTERACTIONS_TYPO (prefix INTERACTIONS_)" in _json(resp)["error"]
    assert ctx.manager.written == []


# -- GET /api/config/mode ----------------------------------------------------


async def test_read_mode(install, monkeypatch):
    install({})
    monkeypatch.setattr(config_ops, "config_mode", lambda: "external")
    resp = await router.read_mode(_req())
    assert resp.status_code == 200
    assert _json(resp) == {"data": {"config_mode": "external"}}
