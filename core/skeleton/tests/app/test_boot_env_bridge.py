"""The cold-boot env bridge: the stored env applied into the process before the manifest resolves.

``read_boot_manifest`` applies the stored env through the SAME apply-then-reset primitive the
reload path uses, so boot and reload resolve the same input to the same ``os.environ``: a
store-only var reaches the serving process and the manifest marker that references it; a key
the deployment env sets keeps the deployment's value, its stored copy ignored and named in one
WARNING; removing a stored key never removes a deployment value; and a marker that dangles
nowhere fires one loud WARNING without crashing the boot.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest
from pyaml_env import parse_config
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.data import load_manifest

from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.instance import app, register_cli_process_state
from tai42_skeleton.config.deployment_env import (
    DeploymentOwnership,
    capture_deployment_env,
    reset_deployment_env,
)
from tai42_skeleton.manifest import Manifest

_MCP_MARKER_MANIFEST = """default_routers: none
mcp:
  - title: svc
    config:
      url: http://svc.invalid/mcp
      headers:
        Authorization: !ENV ${MYTOKEN}
"""


@pytest.fixture(autouse=True)
def _restore_process_env():
    """Snapshot ``os.environ`` + the epoch env-key tracker around each test: the boot
    bridge writes real process env, so restore it and reset the settings caches so no
    applied value leaks into another suite."""
    from tai42_skeleton.app import applied_env

    snapshot = dict(os.environ)
    loaded_before = set(applied_env._loaded_env_keys)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = loaded_before
    reset_all_settings()


@pytest.fixture(autouse=True)
def _bus_of_its_own(monkeypatch):
    """The boot bridge builds the app's worker bus; the bus a test's bridge built goes with the test."""
    monkeypatch.setattr(app, "_bus", None)


def _patch_store(monkeypatch, *, env: dict[str, str], manifest_yaml: str) -> None:
    """Point the app's config manager at an in-memory store + manifest, resolving the
    manifest's ``!ENV`` markers against ``os.environ`` live at read time (the real
    ``read_manifest`` behavior) so a bridged var actually resolves."""
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: dict(env))
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: parse_config(data=manifest_yaml))
    monkeypatch.setattr(app.config.config_manager, "read_manifest_preserved", lambda: load_manifest(manifest_yaml))


def test_store_only_var_is_bridged_and_the_marker_resolves(monkeypatch):
    """A var that lives only in the store lands in ``os.environ`` at boot, and the
    manifest marker referencing it resolves to the real value — never ``"N/A"``."""
    monkeypatch.delenv("MYTOKEN", raising=False)
    _patch_store(monkeypatch, env={"MYTOKEN": "realvalue"}, manifest_yaml=_MCP_MARKER_MANIFEST)

    manifest = asyncio.run(app.lifecycle.read_boot_manifest())

    assert os.environ["MYTOKEN"] == "realvalue"
    assert manifest.mcp_map["svc"].config.headers["Authorization"] == "realvalue"


def test_boot_refuses_an_unknown_nested_key_naming_it(monkeypatch):
    """A misspelled key inside a manifest block fails the boot read loudly, naming the key."""
    from pydantic import ValidationError

    _patch_store(monkeypatch, env={}, manifest_yaml="default_routers: none\napi_tools:\n  expose_destrutive: false\n")

    with pytest.raises(ValidationError, match=r"api_tools\.expose_destrutive\n  Extra inputs are not permitted"):
        asyncio.run(app.lifecycle.read_boot_manifest())


def test_bridged_var_survives_into_app_context(monkeypatch):
    """The bridge applied at the door persists through ``app_context`` boot: the
    store-only var is still live in ``os.environ`` once the context is entered, and
    the epoch env-key tracker carries it (``install_boot_core`` must not clobber it,
    or a later reload's removed-key reconciliation would miss it)."""
    from tai42_skeleton.app import applied_env

    monkeypatch.delenv("BOOT_ONLY", raising=False)
    _patch_store(monkeypatch, env={"BOOT_ONLY": "live"}, manifest_yaml="default_routers: none\n")

    async def run() -> None:
        manifest = await app.lifecycle.read_boot_manifest()
        assert os.environ["BOOT_ONLY"] == "live"
        async with app.app_context(manifest):
            assert os.environ["BOOT_ONLY"] == "live"
            assert "BOOT_ONLY" in applied_env._loaded_env_keys

    asyncio.run(run())


def test_marker_dangling_nowhere_warns_and_boot_proceeds(monkeypatch, caplog):
    """A required marker whose var is in NEITHER the container env nor the store fires
    one loud WARNING naming the var, and boot still returns a manifest — the warning
    kills the silent-phantom state without crashing the boot."""
    monkeypatch.delenv("MYTOKEN", raising=False)
    _patch_store(monkeypatch, env={"SOMETHING_ELSE": "x"}, manifest_yaml=_MCP_MARKER_MANIFEST)

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.app.lifecycle"):
        manifest = asyncio.run(app.lifecycle.read_boot_manifest())

    assert manifest.mcp_map["svc"].title == "svc"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("MYTOKEN" in msg and "N/A" in msg for msg in warnings), warnings


def _seed_container_env(applied_env) -> None:
    """Model a fresh process: the container env, no stored key applied, nothing captured yet."""
    os.environ["SHARED"] = "from-container"
    os.environ["CONTAINER_ONLY"] = "c"
    os.environ["EMPTY_IN_CONTAINER"] = ""
    os.environ.pop("STORE_ONLY", None)
    applied_env._loaded_env_keys = set()
    reset_deployment_env()


def _booted_container_env(applied_env) -> None:
    """Model a process the boot env bridge started: the container env captured as the deployment env."""
    _seed_container_env(applied_env)
    capture_deployment_env()


def test_boot_apply_and_reload_apply_keep_the_deployment_value(monkeypatch):
    """Boot-apply and the reload primitive resolve the same input to the same ``os.environ``:
    a key the deployment env sets keeps the deployment's value over its stored copy on both, a
    store-only var is applied, an empty container value is the stored env's to fill, and a
    container-only var is untouched."""
    from tai42_skeleton.app import applied_env

    stored = {"SHARED": "from-store", "STORE_ONLY": "s", "EMPTY_IN_CONTAINER": "from-store"}
    observed = ("SHARED", "STORE_ONLY", "CONTAINER_ONLY", "EMPTY_IN_CONTAINER")

    _seed_container_env(applied_env)
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: dict(stored))
    asyncio.run(app._apply_stored_env_at_boot(kind=WorkerKind.serve))
    boot_outcome = {k: os.environ.get(k) for k in observed}

    _booted_container_env(applied_env)
    applied_env._apply_env(dict(stored), DeploymentOwnership())
    reload_outcome = {k: os.environ.get(k) for k in observed}

    assert boot_outcome == reload_outcome
    assert boot_outcome == {
        "SHARED": "from-container",
        "STORE_ONLY": "s",
        "CONTAINER_ONLY": "c",
        "EMPTY_IN_CONTAINER": "from-store",
    }
    assert applied_env.applied_env_keys() == frozenset({"STORE_ONLY", "EMPTY_IN_CONTAINER"})


def test_a_later_reload_keeps_the_deployment_value_too(monkeypatch):
    """A reload after boot reads the same stored copy and still keeps the deployment's value."""
    from tai42_skeleton.app import applied_env

    _booted_container_env(applied_env)
    applied_env._apply_env({"STORE_ONLY": "s"}, DeploymentOwnership())
    applied_env._apply_env({"SHARED": "from-store", "STORE_ONLY": "s2"}, DeploymentOwnership())

    assert os.environ["SHARED"] == "from-container"
    assert os.environ["STORE_ONLY"] == "s2"


def test_a_stored_copy_of_a_deployment_key_is_ignored_and_named(monkeypatch, caplog):
    """The ignored stored copy is named in one WARNING — its name only, never a value."""
    from tai42_skeleton.app import applied_env

    _booted_container_env(applied_env)
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.app.epoch"):
        applied_env._apply_env({"SHARED": "secret-stored-value", "STORE_ONLY": "s"}, DeploymentOwnership())

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "SHARED" in warnings[0]
    assert "STORE_ONLY" not in warnings[0]
    assert "secret-stored-value" not in warnings[0]


def test_removing_a_stored_key_never_removes_a_deployment_value():
    """A stored key removed from the store is dropped from the process, but a key the
    deployment sets keeps its value, and a stored value over an empty container value
    gives the container's empty value back."""
    from tai42_skeleton.app import applied_env

    _booted_container_env(applied_env)
    applied_env._apply_env(
        {"SHARED": "from-store", "STORE_ONLY": "s", "EMPTY_IN_CONTAINER": "from-store"}, DeploymentOwnership()
    )
    applied_env._apply_env({}, DeploymentOwnership())

    assert os.environ["SHARED"] == "from-container"
    assert "STORE_ONLY" not in os.environ
    assert os.environ["EMPTY_IN_CONTAINER"] == ""
    assert applied_env.applied_env_keys() == frozenset()


def test_absent_store_is_an_empty_store_not_a_boot_fault(monkeypatch):
    """A genuinely absent store (the managers' shared ``FileNotFoundError``) is an
    empty store — zero keys applied, boot proceeds — never a crash."""

    def _raise_absent() -> dict[str, str]:
        raise FileNotFoundError("no .env")

    monkeypatch.setattr(app.config.config_manager, "read_env", _raise_absent)
    monkeypatch.setattr(
        app.config.config_manager, "read_manifest", lambda: parse_config(data="default_routers: none\n")
    )
    monkeypatch.setattr(
        app.config.config_manager, "read_manifest_preserved", lambda: load_manifest("default_routers: none\n")
    )

    manifest = asyncio.run(app.lifecycle.read_boot_manifest())

    assert isinstance(manifest, Manifest)


_LATE_SETTINGS_MODULE = "tests.app._fixtures.late_settings"


@pytest.fixture
def _late_settings_unregistered(monkeypatch):
    """Keep the late fixture group out of the registry and out of ``sys.modules`` past the test."""
    import sys

    from tai42_kit.settings import registry as settings_registry

    monkeypatch.setattr(settings_registry, "_REGISTRY", dict(settings_registry._REGISTRY))
    monkeypatch.delitem(sys.modules, _LATE_SETTINGS_MODULE, raising=False)
    yield
    sys.modules.pop(_LATE_SETTINGS_MODULE, None)


@pytest.mark.usefixtures("_late_settings_unregistered")
def test_a_group_registered_after_the_boot_apply_keeps_the_deployment_value(monkeypatch, caplog):
    """A plugin group registers inside ``app_context``, after the boot bridge applied the stored
    env. The deployment sets the field under one accepted name and the store holds the field's
    other, preferred name: the deployment's value is the setting at boot and at the first
    reload, and the ignored stored name is logged."""
    from tai42_skeleton.app import applied_env

    stored = {"LATE_ALIASED_ENDPOINT": "from-store"}
    monkeypatch.setenv("LATE_ALIASED_URL", "from-deployment")
    monkeypatch.delenv("LATE_ALIASED_ENDPOINT", raising=False)
    applied_env._loaded_env_keys = set()
    reset_deployment_env()
    _patch_store(
        monkeypatch,
        env=stored,
        manifest_yaml=f"default_routers: none\nlifecycle_modules:\n  - {_LATE_SETTINGS_MODULE}\n",
    )

    def endpoint() -> str | None:
        import sys

        return sys.modules[_LATE_SETTINGS_MODULE].LateAliasedSettings().endpoint

    async def run() -> tuple[str | None, str | None]:
        manifest = await app.lifecycle.read_boot_manifest()
        async with app.app_context(manifest):
            at_boot = endpoint()
            applied_env.apply_env_and_reset_settings(dict(stored), DeploymentOwnership())
            return at_boot, endpoint()

    with caplog.at_level(logging.WARNING):
        at_boot, at_reload = asyncio.run(run())

    assert (at_boot, at_reload) == ("from-deployment", "from-deployment")
    assert "LATE_ALIASED_ENDPOINT" not in applied_env.applied_env_keys()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("LATE_ALIASED_ENDPOINT" in msg and "from-store" not in msg for msg in warnings), warnings


@pytest.mark.usefixtures("_late_settings_unregistered")
def test_a_group_registered_by_a_discarded_build_keeps_the_deployment_value(monkeypatch):
    """A proof build imports a plugin group for the first time and is discarded: the env goes back
    to the live one, except that the stored copy the new group makes the deployment's stays
    withdrawn, so the live process reads the deployment's value."""
    import importlib
    import sys

    from tai42_skeleton.app import applied_env
    from tai42_skeleton.app import epoch as epoch_mod

    stored = {"LATE_ALIASED_ENDPOINT": "from-store"}
    monkeypatch.setenv("LATE_ALIASED_URL", "from-deployment")
    monkeypatch.delenv("LATE_ALIASED_ENDPOINT", raising=False)
    applied_env._loaded_env_keys = set()
    reset_deployment_env()
    _patch_store(monkeypatch, env=stored, manifest_yaml="default_routers: none\n")

    class _Serving:
        async def __call__(self, scope, receive, send) -> None:
            raise AssertionError("never served")

    async def build(_epoch) -> _Serving:
        return _Serving()

    async def run() -> str | None:
        manifest = await app.lifecycle.read_boot_manifest()
        async with app.app_context(manifest):
            assert os.environ["LATE_ALIASED_ENDPOINT"] == "from-store"
            await epoch_mod.build_and_discard_epoch(
                dict(stored),
                rebuild=lambda: importlib.import_module(_LATE_SETTINGS_MODULE),
                build_serving_app=build,
                ownership=DeploymentOwnership(),
            )
            return sys.modules[_LATE_SETTINGS_MODULE].LateAliasedSettings().endpoint

    assert asyncio.run(run()) == "from-deployment"
    assert "LATE_ALIASED_ENDPOINT" not in applied_env.applied_env_keys()


# -- settings a process read before the bridge take effect from the applied env --


def test_boot_refuses_a_stored_env_carrying_an_x_band_key_naming_it(monkeypatch):
    """A hand-written stored config-dir pointer is deployment identity no store carries: the boot
    read refuses it, naming the key, before any stored key reaches ``os.environ``."""
    monkeypatch.delenv("STORE_ONLY", raising=False)
    _patch_store(
        monkeypatch, env={"TAI_CONFIG_DIR_PATH": "/other", "STORE_ONLY": "s"}, manifest_yaml="default_routers: none\n"
    )
    before = dict(os.environ)

    with pytest.raises(ValueError, match=r"X-band\) keys no profile or editor may set: TAI_CONFIG_DIR_PATH\."):
        asyncio.run(app.lifecycle.read_boot_manifest())

    assert dict(os.environ) == before


def test_a_reload_refuses_a_stored_env_carrying_an_x_band_key_naming_it():
    """The reload's apply refuses the same stored env the same way, leaving ``os.environ`` as it was."""
    from tai42_skeleton.app import applied_env

    _booted_container_env(applied_env)
    applied_env._apply_env({"STORE_ONLY": "s"}, DeploymentOwnership())
    before = dict(os.environ)

    with pytest.raises(ValueError, match=r"may set: TAI_CONFIG_MODE\."):
        applied_env.apply_env_and_reset_settings({"STORE_ONLY": "s2", "TAI_CONFIG_MODE": "file"}, DeploymentOwnership())

    assert dict(os.environ) == before
    assert applied_env.applied_env_keys() == frozenset({"STORE_ONLY"})


def test_a_reload_refuses_a_lower_case_spelling_of_an_x_band_key():
    """The settings layer reads ``app_args_port`` as ``APP_ARGS_PORT``: the apply refuses it the same way."""
    from tai42_skeleton.app import applied_env

    _booted_container_env(applied_env)
    before = dict(os.environ)

    with pytest.raises(ValueError, match=r"may set: app_args_port\."):
        applied_env.apply_env_and_reset_settings({"app_args_port": "9999"}, DeploymentOwnership())

    assert dict(os.environ) == before


_LATE_EXCLUDED_MODULE = "tests.app._fixtures.late_excluded_settings"


@pytest.fixture
def _late_excluded_unregistered(monkeypatch):
    """Keep the late excluded fixture group out of the registry and out of ``sys.modules`` past the test."""
    import sys

    from tai42_kit.settings import registry as settings_registry

    monkeypatch.setattr(settings_registry, "_REGISTRY", dict(settings_registry._REGISTRY))
    monkeypatch.delitem(sys.modules, _LATE_EXCLUDED_MODULE, raising=False)
    yield
    sys.modules.pop(_LATE_EXCLUDED_MODULE, None)


@pytest.mark.usefixtures("_late_excluded_unregistered")
def test_a_group_registered_after_the_apply_refuses_an_applied_key_it_makes_x_band():
    """A plugin group registered after the stored env was applied declares an ``excluded`` field
    whose env name the store carried: its registration is refused, naming the key."""
    import importlib

    from tai42_skeleton.app import applied_env

    _booted_container_env(applied_env)
    os.environ.pop("LATE_EXCLUDED_IDENTITY", None)
    applied_env._apply_env({"LATE_EXCLUDED_IDENTITY": "stored", "STORE_ONLY": "s"}, DeploymentOwnership())

    with pytest.raises(ValueError, match=r"may set: LATE_EXCLUDED_IDENTITY\."):
        importlib.import_module(_LATE_EXCLUDED_MODULE)


@pytest.fixture
def _handlers_of_its_own(monkeypatch):
    """Every lifecycle handler registry of the app, as it stood before the test, comes back after it."""
    for name, value in list(vars(app).items()):
        if name.endswith("_handlers") and isinstance(value, dict):
            monkeypatch.setattr(app, name, dict(value))


@pytest.fixture
def own_root_logger():
    """The root logger's handlers and level, as they stood before the test, come back after it."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield root
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.mark.usefixtures("_handlers_of_its_own")
def test_a_stored_log_level_sets_the_root_logger_of_a_cli_process_at_boot(monkeypatch, own_root_logger):
    """A CLI process configures its root logger before the bridge; a stored ``TAI_LOG_LEVEL``
    the deployment does not set is the level the process logs at once the bridge applied it."""
    from tai42_kit.logging import logging_settings, setup_logging

    import tai42_skeleton.app.instance as instance

    monkeypatch.delenv("TAI_LOG_LEVEL", raising=False)
    reset_all_settings()
    setup_logging(logging_settings())
    instance.register_cli_process_state()
    assert own_root_logger.level == logging.INFO
    _patch_store(monkeypatch, env={"TAI_LOG_LEVEL": "DEBUG"}, manifest_yaml="default_routers: none\n")

    asyncio.run(app.lifecycle.read_boot_manifest())

    assert own_root_logger.level == logging.DEBUG


@pytest.mark.usefixtures("_handlers_of_its_own")
def test_a_stored_connector_meta_token_key_is_the_key_the_log_redactor_masks(monkeypatch):
    """The connector-secret log rule is registered before the bridge; a stored
    ``CONNECTORS_META_TOKEN_KEY`` is the key it masks once the bridge applied it."""
    from tai42_kit.logging import redaction as kit_redaction

    from tai42_skeleton.connectors.meta_log_redactor import register_connector_log_redaction

    monkeypatch.setattr(kit_redaction, "_RECORD_REDACTORS", kit_redaction._Registry())
    monkeypatch.delenv("CONNECTORS_META_TOKEN_KEY", raising=False)
    reset_all_settings()
    register_connector_log_redaction()
    stored = {"CONNECTORS_META_TOKEN_KEY": "stored_hub.access_token"}
    _patch_store(monkeypatch, env=stored, manifest_yaml="default_routers: none\n")

    asyncio.run(app.lifecycle.read_boot_manifest())

    rule = kit_redaction._RECORD_REDACTORS._by_name["connector-secrets"]
    assert "stored_hub.access_token" in rule.markers
    assert rule.redact('{"stored_hub.access_token": "s3cr3t-value"}').count("s3cr3t-value") == 0


@pytest.fixture
def _connector_rule_of_its_own(monkeypatch):
    """A fresh record-redactor registry holding the connector-secret rule on the default key."""
    from tai42_kit.logging import redaction as kit_redaction

    from tai42_skeleton.connectors.meta_log_redactor import register_connector_log_redaction

    monkeypatch.setattr(kit_redaction, "_RECORD_REDACTORS", kit_redaction._Registry())
    monkeypatch.delenv("CONNECTORS_META_TOKEN_KEY", raising=False)
    reset_all_settings()
    register_connector_log_redaction()


def _connector_rule_masks(key: str) -> bool:
    from tai42_kit.logging import redaction as kit_redaction

    rule = kit_redaction._RECORD_REDACTORS._by_name["connector-secrets"]
    return key in rule.markers and "s3cr3t-value" not in rule.redact(f'{{"{key}": "s3cr3t-value"}}')


@pytest.mark.usefixtures("_connector_rule_of_its_own")
def test_a_reload_naming_a_new_connector_meta_token_key_is_the_key_the_log_redactor_masks():
    """``CONNECTORS_META_TOKEN_KEY`` changes in place on a reload: once the reload applied the
    new key, the connector-secret log rule masks the value under it."""
    from tai42_skeleton.app import applied_env

    _booted_container_env(applied_env)

    applied_env.apply_env_and_reset_settings({"CONNECTORS_META_TOKEN_KEY": "reloaded_hub.token"}, DeploymentOwnership())

    assert _connector_rule_masks("reloaded_hub.token")


@pytest.mark.usefixtures("_connector_rule_of_its_own")
def test_a_discarded_reload_leaves_the_log_redactor_on_the_live_connector_meta_token_key():
    """A reload build that is discarded restores the live env: the connector-secret log rule
    masks the live key again, the key the connectors keep writing tokens under."""
    from tai42_skeleton.app import applied_env
    from tai42_skeleton.app import epoch as epoch_mod

    _booted_container_env(applied_env)
    seen_during_build: list[bool] = []

    class _Serving:
        async def __call__(self, scope, receive, send) -> None:
            raise AssertionError("never served")

    async def build(_epoch) -> _Serving:
        seen_during_build.append(_connector_rule_masks("reloaded_hub.token"))
        return _Serving()

    asyncio.run(
        epoch_mod.build_and_discard_epoch(
            {"CONNECTORS_META_TOKEN_KEY": "reloaded_hub.token"},
            rebuild=lambda: None,
            build_serving_app=build,
            ownership=DeploymentOwnership(),
        )
    )

    assert seen_during_build == [True]
    assert _connector_rule_masks("tai_hub.access_token")


@pytest.fixture
def _fastmcp_settings_of_its_own():
    """fastmcp's process-wide settings and its logger, as they stood before the test, come back after it."""
    import copy

    import fastmcp

    saved = copy.deepcopy(fastmcp.settings)
    logger = logging.getLogger("fastmcp")
    saved_handlers, saved_level, saved_propagate = logger.handlers[:], logger.level, logger.propagate
    yield
    for name in type(saved).model_fields:
        fastmcp.settings.set_setting(name, getattr(saved, name))
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate


def _file_mode_config_dir(monkeypatch, tmp_path, stored_file: str) -> None:
    """A ``file`` mode config dir that is the working directory, its stored env file holding ``stored_file``."""
    (tmp_path / ".env").write_text(stored_file)
    monkeypatch.setenv("TAI_CONFIG_MODE", "file")
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _fastmcp_import_read() -> None:
    """The read fastmcp makes of its settings when it is imported in the working directory."""
    import fastmcp
    from fastmcp.settings import Settings

    imported = Settings()
    for name in Settings.model_fields:
        fastmcp.settings.set_setting(name, getattr(imported, name))


@pytest.mark.usefixtures("_handlers_of_its_own", "_fastmcp_settings_of_its_own")
def test_fastmcp_never_serves_a_value_read_from_the_stored_env_file(monkeypatch, tmp_path):
    """fastmcp reads the working directory's ``.env`` when it is imported; in the ``file`` mode that
    file is the stored env. Once the bridge has applied the stored env in a CLI process, a stored
    key the bridge did not apply is not a value fastmcp serves with."""
    import fastmcp

    monkeypatch.delenv("FASTMCP_STREAMABLE_HTTP_PATH", raising=False)
    _file_mode_config_dir(monkeypatch, tmp_path, "FASTMCP_STREAMABLE_HTTP_PATH=/stored-path/\n")
    _fastmcp_import_read()
    assert fastmcp.settings.streamable_http_path == "/stored-path/"
    _patch_store(monkeypatch, env={}, manifest_yaml="default_routers: none\n")

    register_cli_process_state()

    asyncio.run(app.lifecycle.read_boot_manifest())

    assert fastmcp.settings.streamable_http_path == "/mcp"


@pytest.mark.usefixtures("_handlers_of_its_own", "_fastmcp_settings_of_its_own")
def test_a_stored_fastmcp_key_the_bridge_applied_is_the_value_fastmcp_serves(monkeypatch):
    """fastmcp reads its settings when it is imported, before the bridge; in a CLI process a stored
    ``FASTMCP_*`` key the bridge applies is the value fastmcp serves with once the bridge has applied it."""
    import fastmcp

    monkeypatch.delenv("FASTMCP_STREAMABLE_HTTP_PATH", raising=False)
    _patch_store(
        monkeypatch, env={"FASTMCP_STREAMABLE_HTTP_PATH": "/applied-path/"}, manifest_yaml="default_routers: none\n"
    )

    register_cli_process_state()

    asyncio.run(app.lifecycle.read_boot_manifest())

    assert fastmcp.settings.streamable_http_path == "/applied-path/"


@pytest.mark.usefixtures("_handlers_of_its_own", "_fastmcp_settings_of_its_own")
@pytest.mark.parametrize(
    ("stored", "served"),
    [
        ({"FASTMCP_DOCKET_URL": "redis://applied"}, "redis://applied"),
        ({"FASTMCP_DOCKET__URL": "redis://applied"}, "redis://applied"),
        ({"FASTMCP_DOCKET_URL": "redis://applied", "FASTMCP_DOCKET__CONCURRENCY": "3"}, "redis://applied"),
        ({}, "memory://"),
    ],
)
def test_a_stored_fastmcp_docket_key_the_bridge_applied_is_the_docket_value_fastmcp_serves(monkeypatch, stored, served):
    """fastmcp's docket settings are a settings group of their own, read with their own names
    (``FASTMCP_DOCKET_URL``) as well as the delimited names of fastmcp's settings; a stored docket
    key the bridge applies is the value fastmcp serves with, and with none stored the docket runs
    its default."""
    import fastmcp

    for name in ("FASTMCP_DOCKET_URL", "FASTMCP_DOCKET__URL", "FASTMCP_DOCKET"):
        monkeypatch.delenv(name, raising=False)
    fastmcp.settings.set_setting("docket", type(fastmcp.settings.docket)(url="redis://imported"))
    _patch_store(monkeypatch, env=stored, manifest_yaml="default_routers: none\n")

    register_cli_process_state()

    asyncio.run(app.lifecycle.read_boot_manifest())

    assert fastmcp.settings.docket.url == served


@pytest.mark.usefixtures("_handlers_of_its_own", "_fastmcp_settings_of_its_own")
def test_a_launch_env_file_in_the_working_directory_is_still_read_by_fastmcp(monkeypatch, tmp_path):
    """A working-directory ``.env`` that is not the stored env (the config dir is elsewhere) stays
    a source fastmcp reads its settings from in a CLI process."""
    import fastmcp

    monkeypatch.delenv("FASTMCP_STREAMABLE_HTTP_PATH", raising=False)
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / ".env").write_text("FASTMCP_STREAMABLE_HTTP_PATH=/launch-path/\n")
    monkeypatch.setenv("TAI_CONFIG_MODE", "file")
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(tmp_path / "config"))
    monkeypatch.chdir(launch)
    _patch_store(monkeypatch, env={}, manifest_yaml="default_routers: none\n")

    register_cli_process_state()

    asyncio.run(app.lifecycle.read_boot_manifest())

    assert fastmcp.settings.streamable_http_path == "/launch-path/"


@pytest.mark.usefixtures("_handlers_of_its_own", "_fastmcp_settings_of_its_own", "own_root_logger")
def test_a_stored_fastmcp_log_level_sets_the_fastmcp_logger_of_a_cli_process_at_boot(monkeypatch):
    """fastmcp configures its own logger when it is imported; in a CLI process a stored
    ``FASTMCP_LOG_LEVEL`` the bridge applies is the level that logger logs at."""
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    register_cli_process_state()
    _patch_store(monkeypatch, env={"FASTMCP_LOG_LEVEL": "DEBUG"}, manifest_yaml="default_routers: none\n")

    asyncio.run(app.lifecycle.read_boot_manifest())

    assert logging.getLogger("fastmcp").level == logging.DEBUG


@pytest.mark.usefixtures("_handlers_of_its_own", "_fastmcp_settings_of_its_own")
def test_an_embedded_app_leaves_the_host_s_fastmcp_settings_as_the_host_set_them(monkeypatch):
    """fastmcp's settings are process-wide: an embedded app's boot leaves every value the host set."""
    import fastmcp

    fastmcp.settings.set_setting("mask_error_details", True)
    _patch_store(monkeypatch, env={"FASTMCP_MASK_ERROR_DETAILS": "false"}, manifest_yaml="default_routers: none\n")

    asyncio.run(app.lifecycle.read_boot_manifest())

    assert fastmcp.settings.mask_error_details is True


def _named_handlers(registry: dict, name: str) -> list:
    return [handler for handler in registry.values() if handler.__name__ == name]


def test_a_stored_access_control_disable_skips_the_identity_provider_probe(monkeypatch):
    """The access-control handlers follow the gate's setting as the epoch builds, so a
    stored ``ACCESS_CONTROL_ENABLE=false`` applied at boot does not run the gate's probe."""
    import tai42_skeleton.app.instance as instance

    probes: list[str] = []

    async def probe_identity_provider() -> None:
        probes.append("probed")

    monkeypatch.setattr(instance, "probe_identity_provider", probe_identity_provider)
    monkeypatch.setattr(instance, "_app", None)
    monkeypatch.delenv("ACCESS_CONTROL_ENABLE", raising=False)
    reset_all_settings()
    built = instance.build_app()
    monkeypatch.setattr(built, "_bus", None)
    monkeypatch.setattr(built.config.config_manager, "read_env", lambda: {"ACCESS_CONTROL_ENABLE": "false"})
    monkeypatch.setattr(built.config.config_manager, "read_manifest", lambda: {"default_routers": "none"})
    monkeypatch.setattr(built.config.config_manager, "read_manifest_preserved", lambda: {"default_routers": "none"})

    asyncio.run(built.lifecycle.read_boot_manifest())
    for handler in _named_handlers(built._startup_handlers, "probe_identity_provider"):
        asyncio.run(handler())

    assert probes == []


def test_access_control_enabled_after_the_build_runs_the_identity_provider_probe(monkeypatch):
    """An app built while the gate is off still probes the identity provider in an epoch
    built once the gate is on."""
    import tai42_skeleton.app.instance as instance

    probes: list[str] = []

    async def probe_identity_provider() -> None:
        probes.append("probed")

    monkeypatch.setattr(instance, "probe_identity_provider", probe_identity_provider)
    monkeypatch.setattr(instance, "_app", None)
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    reset_all_settings()
    built = instance.build_app()
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "true")
    reset_all_settings()

    for handler in _named_handlers(built._startup_handlers, "probe_identity_provider"):
        asyncio.run(handler())

    assert probes == ["probed"]
