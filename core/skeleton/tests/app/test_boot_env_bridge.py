"""The cold-boot env bridge: a manifest ``!ENV ${VAR}`` whose var lives only in the
persisted store resolves to its real value at boot, not the silent ``"N/A"`` sentinel.

Boot never applied the stored env into ``os.environ`` before the manifest was
resolved and the MCP mounts probed — only a full reload did — so a store-only marker
resolved to ``"N/A"`` until the first reload. These pin the fix: ``read_boot_manifest``
bridges the store through the SAME apply-then-reset primitive the reload path uses,
so boot and reload resolve settings under the store identically (stored env OVERRIDES
container env on both), a store-only var survives into the serving process, a marker
that dangles nowhere fires one loud WARNING without crashing the boot, and the two
paths' precedence is bit-identical.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os

import pytest
from pyaml_env import parse_config
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.data import load_manifest

from tai42_skeleton.app.instance import app
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
    from tai42_skeleton.app import epoch as epoch_mod

    snapshot = dict(os.environ)
    loaded_before = set(epoch_mod._loaded_env_keys)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = loaded_before
    reset_all_settings()


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

    manifest = app.lifecycle.read_boot_manifest()

    assert os.environ["MYTOKEN"] == "realvalue"
    assert manifest.mcp_map["svc"].config.headers["Authorization"] == "realvalue"


def test_bridged_var_survives_into_app_context(monkeypatch):
    """The bridge applied at the door persists through ``app_context`` boot: the
    store-only var is still live in ``os.environ`` once the context is entered, and
    the epoch env-key tracker carries it (``install_boot_core`` must not clobber it,
    or a later reload's removed-key reconciliation would miss it)."""
    from tai42_skeleton.app import epoch as epoch_mod

    monkeypatch.delenv("BOOT_ONLY", raising=False)
    _patch_store(monkeypatch, env={"BOOT_ONLY": "live"}, manifest_yaml="default_routers: none\n")

    async def run() -> None:
        manifest = app.lifecycle.read_boot_manifest()
        assert os.environ["BOOT_ONLY"] == "live"
        async with app.app_context(manifest):
            assert os.environ["BOOT_ONLY"] == "live"
            assert "BOOT_ONLY" in epoch_mod._loaded_env_keys

    asyncio.run(run())


def test_k8s_secret_env_is_bridged_at_boot(monkeypatch):
    """The k8s-mode config manager path: a var stored in the K8s Secret is decoded,
    bridged into ``os.environ``, and its manifest marker resolves — the boot bridge is
    polymorphic over the active config manager."""
    # The k8s client is the plugin's optional ``k8s`` extra, absent from the
    # skeleton dev closure; skip rather than error when it is not installed.
    pytest.importorskip("kubernetes")
    from pathlib import Path

    from kubernetes import client
    from tai42_config_k8s import settings as settings_mod
    from tai42_config_k8s.manager import K8sConfigManager

    monkeypatch.delenv("MYTOKEN", raising=False)
    # No service-account file -> the settings resolve the default namespace.
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", Path("/nonexistent/tai-sa-namespace"))
    reset_all_settings()

    class _FakeCoreApi:
        def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret:
            data = {"MYTOKEN": base64.b64encode(b"k8s-real").decode("utf-8")}
            return client.V1Secret(data=data, metadata=client.V1ObjectMeta(name=name))

    manager = K8sConfigManager()
    manager._core_api = _FakeCoreApi()  # type: ignore[attr-defined]
    monkeypatch.setattr(manager, "read_manifest", lambda: parse_config(data=_MCP_MARKER_MANIFEST))
    monkeypatch.setattr(manager, "read_manifest_preserved", lambda: load_manifest(_MCP_MARKER_MANIFEST))
    monkeypatch.setattr(app, "_config_manager", manager)

    manifest = app.lifecycle.read_boot_manifest()

    assert os.environ["MYTOKEN"] == "k8s-real"
    assert manifest.mcp_map["svc"].config.headers["Authorization"] == "k8s-real"


def test_marker_dangling_nowhere_warns_and_boot_proceeds(monkeypatch, caplog):
    """A required marker whose var is in NEITHER the container env nor the store fires
    one loud WARNING naming the var, and boot still returns a manifest — the warning
    kills the silent-phantom state without crashing the boot."""
    monkeypatch.delenv("MYTOKEN", raising=False)
    _patch_store(monkeypatch, env={"SOMETHING_ELSE": "x"}, manifest_yaml=_MCP_MARKER_MANIFEST)

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.app.lifecycle"):
        manifest = app.lifecycle.read_boot_manifest()

    assert manifest.mcp_map["svc"].title == "svc"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("MYTOKEN" in msg and "N/A" in msg for msg in warnings), warnings


def test_boot_apply_and_reload_apply_have_identical_precedence(monkeypatch):
    """Boot-apply and the reload primitive resolve the same input to the same
    ``os.environ``: stored env OVERRIDES container env on both, and a container-only
    var is untouched — the consistency invariant the fix guarantees."""
    from tai42_skeleton.app import epoch as epoch_mod

    stored = {"SHARED": "from-store", "STORE_ONLY": "s"}
    observed = ("SHARED", "STORE_ONLY", "CONTAINER_ONLY")

    def _seed_base_env() -> None:
        os.environ["SHARED"] = "from-container"
        os.environ["CONTAINER_ONLY"] = "c"
        os.environ.pop("STORE_ONLY", None)
        epoch_mod._loaded_env_keys = set()

    _seed_base_env()
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: dict(stored))
    app._apply_stored_env_at_boot()
    boot_outcome = {k: os.environ.get(k) for k in observed}

    _seed_base_env()
    epoch_mod._apply_env(dict(stored))
    reload_outcome = {k: os.environ.get(k) for k in observed}

    assert boot_outcome == reload_outcome
    assert boot_outcome["SHARED"] == "from-store"
    assert boot_outcome["STORE_ONLY"] == "s"
    assert boot_outcome["CONTAINER_ONLY"] == "c"


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

    manifest = app.lifecycle.read_boot_manifest()

    assert isinstance(manifest, Manifest)
