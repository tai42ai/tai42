"""Op-level oracles for the runtime fleet ops: ``update_manifest``, ``reload_mcp``,
``reload_failed_mcps`` and ``deregister_mcp``.

``update_manifest`` persists the whole posted document through the replace pipeline
(``!ENV`` markers intact) and reloads the fleet; a backend-needs-bus or unresolved
connector-env fault maps to a loud 400 before any persist. The reload/deregister ops
apply on this worker when targeted, then broadcast; a local-apply raise aborts before any
broadcast, and an unknown title is a loud 404. ``update_manifest`` is tier-2, off the
default surface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.manifest import ApiToolsConfig
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.operations import BadRequestError, NotFoundError, OperationRegistry, operation_metadata_of
from tai42_skeleton.operations import manifest as manifest_ops
from tai42_skeleton.operations.projection import is_tier2, project_operations

from ..._fakes.bus import FakeBus
from .conftest import _Admin, _install, _install_pipeline, _ReloadAdmin, _ReplaceStore

# -- update_manifest (persist-through via the ConfigService pipeline) ----------


async def test_update_manifest_persists_through_and_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _ReplaceStore()
    admin = _ReloadAdmin({"status": "ok", "env_keys": 2})
    bus = _install_pipeline(monkeypatch, store=store, admin=admin)

    result = await manifest_ops.update_manifest("mcp: []\n")

    # The whole posted document is validated, persisted, reloaded locally, and the
    # reload broadcast to the WHOLE fleet (targets None); a lone worker collapses the
    # fan-out to the local note.
    assert store.replaced == [{"mcp": []}]
    assert admin.calls == 1
    assert bus.publish_calls[0][0] == {"op": "reload_config"}
    assert bus.publish_calls[0][1] is None
    assert result == {
        "status": "ok",
        "env_keys": 2,
        "fanout": {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"},
    }


async def test_update_manifest_persists_env_markers_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    # A marker-carrying document pushed through the replace surface persists with its
    # ``!ENV`` markers INTACT — the resolved value is used only for in-memory
    # validation, so no secret ever bakes to disk.
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("TAI_BACKEND", "myapp.backend")
    reset_all_settings()
    try:
        store = _ReplaceStore()
        _install_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

        await manifest_ops.update_manifest("backend_module: !ENV ${TAI_BACKEND}\n")

        # The marker string survives verbatim — never the resolved ``myapp.backend``.
        assert store.replaced == [{"backend_module": "!ENV ${TAI_BACKEND}"}]
    finally:
        reset_all_settings()


async def test_update_manifest_backend_without_bus_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # A replacement that registers a task backend with no worker bus configured is
    # refused by ConfigService's backend-needs-bus invariant, which raises the
    # RuntimeError ``BackendNeedsBusError`` at MUTATE time. The op must map it to a loud
    # 400 naming TAI_BUS_REDIS_URL, not let it escape as an unhandled 500.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        store = _ReplaceStore()
        _install_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

        with pytest.raises(BadRequestError, match="TAI_BUS_REDIS_URL"):
            await manifest_ops.update_manifest("backend_module: myapp.backend\n")

        assert store.replaced == []  # rejected in validation, before any persist
    finally:
        reset_all_settings()


def _oauth_connector(provider_id: str = "acme") -> dict[str, Any]:
    """A valid oauth ``ProviderDescriptor`` for the manifest ``connectors`` list — its
    ``client_id_env`` / ``client_secret_env`` name the env the connector reads at connect
    time (abstract synthetic provider ids only)."""
    return {
        "id": provider_id,
        "display_name": provider_id.title(),
        "icon_url": f"https://example.com/{provider_id}.png",
        "kind": "oauth",
        "origin": "system",
        "category": "productivity",
        "oauth": {"authorize": "https://auth.example.com/authorize", "token": "https://auth.example.com/token"},
        "client_id_env": f"{provider_id.upper()}_CLIENT_ID",
        "client_secret_env": f"{provider_id.upper()}_CLIENT_SECRET",
        "sub_services": {
            "main": {
                "id": "main",
                "display_name": "Main",
                "scopes": ["read"],
                "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
            }
        },
    }


async def test_update_manifest_oauth_connector_missing_client_env_maps_to_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A replacement carrying a live oauth connector whose client_id_env / client_secret_env
    # resolve to no env var is refused by refuse_unresolved_env's CONNECTOR half (not the
    # dangling-marker half): a ValueError inside the pipeline the op maps to a loud 400
    # naming the unset var, driven end-to-end through the operations-layer door.
    monkeypatch.delenv("ACME_CLIENT_ID", raising=False)
    monkeypatch.delenv("ACME_CLIENT_SECRET", raising=False)
    reset_all_settings()
    try:
        store = _ReplaceStore()
        _install_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

        with pytest.raises(BadRequestError, match="ACME_CLIENT_SECRET"):
            await manifest_ops.update_manifest(json.dumps({"connectors": [_oauth_connector("acme")]}))

        assert store.replaced == []  # refused in validation, before any persist
    finally:
        reset_all_settings()


# -- reload_mcp ---------


async def test_reload_mcp_untargeted_applies_locally_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(
        live_manifest={"mcp": [{"title": "svc"}]},
        results={"reload_mcp": {"title": "svc", "status": "ok", "tools": ["t1"]}},
    )
    bus = _install(monkeypatch, admin=admin)

    result = await manifest_ops.reload_mcp("svc")

    assert admin.calls == [("reload_mcp", "svc")]
    assert bus.publish_calls == [
        (
            {"op": "reload_mcp", "title": "svc"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload={"title": "svc", "status": "ok", "tools": ["t1"]}),
        )
    ]
    assert result["results"][0]["payload"] == {"title": "svc", "status": "ok", "tools": ["t1"]}


async def test_reload_mcp_targeted_to_remote_skips_local(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(
        live_manifest={"mcp": [{"title": "svc"}]},
        results={"reload_mcp": {"title": "svc", "status": "ok"}},
    )
    bus = _install(monkeypatch, admin=admin, bus=FakeBus(remotes=["serve-w1"]))

    result = await manifest_ops.reload_mcp("svc", ["serve-w1"])

    assert admin.calls == []  # self not targeted → no local re-probe
    assert bus.publish_calls == [({"op": "reload_mcp", "title": "svc"}, ["serve-w1"], None)]
    assert {r["name"] for r in result["results"]} == {"serve-w1"}


async def test_reload_mcp_unknown_title_404(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(live_manifest={"mcp": [{"title": "svc"}]})
    bus = _install(monkeypatch, admin=admin)

    with pytest.raises(NotFoundError, match="unknown mcp title"):
        await manifest_ops.reload_mcp("nope")
    # 404 precedes any broadcast.
    assert bus.publish_calls == []


async def test_reload_mcp_unknown_target_raises_before_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(live_manifest={"mcp": [{"title": "svc"}]})
    bus = _install(monkeypatch, admin=admin)

    with pytest.raises(BadRequestError, match="unknown fleet targets"):
        await manifest_ops.reload_mcp("svc", ["ghost"])
    assert admin.calls == []
    assert bus.publish_calls == []


async def test_reload_mcp_local_apply_raise_aborts_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(live_manifest={"mcp": [{"title": "svc"}]}, raise_for="reload_mcp")
    bus = _install(monkeypatch, admin=admin)

    with pytest.raises(RuntimeError, match="reload_mcp failed"):
        await manifest_ops.reload_mcp("svc")
    assert bus.publish_calls == []


# -- reload_failed_mcps -----------------------------


async def test_reload_failed_mcps_untargeted_applies_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(results={"reload_failed_mcps": [{"title": "svc", "status": "ok"}]})
    bus = _install(monkeypatch, admin=admin)

    result = await manifest_ops.reload_failed_mcps()

    assert admin.calls == [("reload_failed_mcps",)]
    assert bus.publish_calls == [
        (
            {"op": "reload_failed_mcps"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=[{"title": "svc", "status": "ok"}]),
        )
    ]
    assert result["results"][0]["payload"] == [{"title": "svc", "status": "ok"}]


# -- deregister_mcp ---------------------------------


async def test_deregister_mcp_untargeted_applies_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(results={"deregister_mcp": {"title": "svc", "status": "ok", "removed": ["svc_t"]}})
    bus = _install(monkeypatch, admin=admin)

    result = await manifest_ops.deregister_mcp("svc")

    assert admin.calls == [("deregister_mcp", "svc")]
    assert bus.publish_calls == [
        (
            {"op": "deregister_mcp", "title": "svc"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload={"title": "svc", "status": "ok", "removed": ["svc_t"]}),
        )
    ]
    assert result["results"][0]["payload"] == {"title": "svc", "status": "ok", "removed": ["svc_t"]}


# -- destructive / reload-gate / tier metadata -------------------------------


def test_mutating_ops_are_destructive_and_reload_gated() -> None:
    for op in (
        manifest_ops.set_mcp_config,
        manifest_ops.reload_mcp,
        manifest_ops.update_manifest,
        manifest_ops.reload_failed_mcps,
        manifest_ops.deregister_mcp,
    ):
        meta = operation_metadata_of(op)
        assert meta.destructive is True, meta.name
        assert meta.reload_gated is True, meta.name


def test_update_manifest_is_tier2_and_off_the_default_surface() -> None:
    update_meta = operation_metadata_of(manifest_ops.update_manifest)
    assert update_meta.authority_changing is True
    assert is_tier2(update_meta) is True

    reg = OperationRegistry()
    for op in (manifest_ops.update_manifest, manifest_ops.reload_mcp):
        reg.register(operation_metadata_of(op))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, Any] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = annotations
            return lambda fn: fn

    app = SimpleNamespace(tools=_Rec())
    # Default surface: update_manifest is off (tier-2); reload_mcp projects with a
    # destructiveHint.
    names = project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)
    assert "update_manifest" not in names
    assert "reload_mcp" in names
    assert app.tools.registered["reload_mcp"].destructiveHint is True

    # Includable via explicit api_tools.include.
    app2 = SimpleNamespace(tools=_Rec())
    names2 = project_operations(
        app2, ApiToolsConfig(include=["update_manifest"], expose_destructive=True), registry=reg
    )
    assert "update_manifest" in names2
    assert app2.tools.registered["update_manifest"].destructiveHint is True
