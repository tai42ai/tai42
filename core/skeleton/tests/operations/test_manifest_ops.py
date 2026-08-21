"""Op-level oracles for the manifest / MCP-status operations.

Covers ``update_manifest``, ``reload_mcp``, ``reload_failed_mcps``,
``list_failed_mcps`` and ``deregister_mcp``. The runtime ops (class a) apply on this
worker when it is a target, then broadcast on the bus; the response is the per-origin
fleet report, and a local-apply raise aborts before anything is broadcast. An unknown
title is a loud ``NotFoundError`` before any broadcast. Tier/destructive/projection
metadata is pinned too (``update_manifest`` is tier-2, off the default surface).
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.manifest import ApiToolsConfig
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.operations import BadRequestError, NotFoundError, OperationRegistry, operation_metadata_of
from tai42_skeleton.operations import manifest as manifest_ops
from tai42_skeleton.operations.projection import is_tier2, project_operations

from .._fakes.bus import FakeBus


class _Admin:
    def __init__(
        self,
        *,
        live_manifest: dict | None = None,
        results: dict[str, object] | None = None,
        raise_for: str | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self.live_manifest = live_manifest if live_manifest is not None else {"mcp": [{"title": "svc"}]}
        self._results = results or {}
        self._raise_for = raise_for

    def _dispatch(self, method: str, *args: object) -> object:
        self.calls.append((method, *args))
        if self._raise_for == method:
            raise RuntimeError(f"{method} failed")
        return self._results.get(method)

    def list_failed_mcps(self) -> object:
        return self._dispatch("list_failed_mcps")

    def reload_mcp(self, title: str) -> object:
        return self._dispatch("reload_mcp", title)

    def reload_failed_mcps(self) -> object:
        return self._dispatch("reload_failed_mcps")

    def deregister_mcp(self, title: str) -> object:
        return self._dispatch("deregister_mcp", title)


def _install(
    monkeypatch: pytest.MonkeyPatch, *, admin: _Admin, backend: object = None, bus: FakeBus | None = None
) -> FakeBus:
    impl = SimpleNamespace(admin=admin, backends=SimpleNamespace(backend=backend))
    monkeypatch.setattr(tai42_app, "_impl", impl)
    bus = bus or FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


# -- update_manifest (persist-through via the ConfigService pipeline) ----------


class _ReplaceStore:
    """A config manager whose ``replace_manifest`` records and persists the whole
    posted document — the seam the update_manifest pipeline drives."""

    def __init__(self, *, manifest: dict | None = None, env: dict | None = None) -> None:
        self.manifest: dict = manifest if manifest is not None else {}
        self.env: dict = env if env is not None else {}
        self.replaced: list[dict] = []

    def replace_manifest(self, document: dict) -> dict:
        self.replaced.append(dict(document))
        self.manifest = dict(document)
        return dict(document)

    def read_manifest_preserved(self) -> dict:
        return dict(self.manifest)

    def read_env(self) -> dict:
        return dict(self.env)


class _MutateStore:
    """A config manager whose ``mutate_manifest`` runs the guarded mutator on a copy of
    the stored document and persists only if it returns without raising — the seam the
    set_mcp_config pipeline drives (a raise inside leaves the store untouched)."""

    def __init__(self, *, manifest: dict | None = None) -> None:
        self.manifest: dict = manifest if manifest is not None else {}
        self.persisted: list[dict] = []

    def mutate_manifest(self, mutator: Any) -> dict:
        document = deepcopy(self.manifest)
        mutator(document)  # a raise here propagates before any persist
        self.manifest = document
        self.persisted.append(deepcopy(document))
        return document

    def read_manifest_preserved(self) -> dict:
        return deepcopy(self.manifest)


class _ReloadAdmin:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {"status": "ok", "env_keys": 0}
        self.calls = 0

    def reload_config(self) -> dict:
        self.calls += 1
        return self.result


def _install_pipeline(
    monkeypatch: pytest.MonkeyPatch, *, store: _ReplaceStore, admin: _ReloadAdmin, backend: object = None
) -> FakeBus:
    impl = SimpleNamespace(
        config=SimpleNamespace(config_manager=store),
        admin=admin,
        backends=SimpleNamespace(backend=backend),
    )
    monkeypatch.setattr(tai42_app, "_impl", impl)
    bus = FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


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


# -- set_mcp_config (persist-through via the ConfigService pipeline) -----------


def _install_mutate_pipeline(monkeypatch: pytest.MonkeyPatch, *, store: _MutateStore, admin: _ReloadAdmin) -> FakeBus:
    impl = SimpleNamespace(
        config=SimpleNamespace(config_manager=store),
        admin=admin,
        backends=SimpleNamespace(backend=None),
    )
    monkeypatch.setattr(tai42_app, "_impl", impl)
    bus = FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


async def test_set_mcp_config_backend_without_bus_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # The already-registered backend plus the new mcp section resolves to a config that
    # needs the bus; with none configured, ConfigService raises the RuntimeError
    # ``BackendNeedsBusError`` at MUTATE time. The op must map it to a loud 400 naming
    # TAI_BUS_REDIS_URL, not let it escape as an unhandled 500.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        store = _MutateStore(manifest={"backend_module": "myapp.backend"})
        _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

        with pytest.raises(BadRequestError, match="TAI_BUS_REDIS_URL"):
            await manifest_ops.set_mcp_config([])

        assert store.persisted == []  # rejected in validation, before any persist
    finally:
        reset_all_settings()


# -- add/remove entries (mcp / tools / agents) + api_tools lists ---------------


def _mcp_entry(title: str, url: str = "https://example.com/mcp") -> dict:
    return {"title": title, "config": {"type": "streamable_http", "url": url}}


def _tools_entry(title: str, module: str | None = None) -> dict:
    # The manifest validator refuses two rows sharing a module, so a per-title default
    # keeps distinct entries distinct.
    return {"title": title, "module": module or f"pkg.{title}"}


def _agents_entry(title: str, module: str | None = None) -> dict:
    return {"title": title, "module": module or f"pkg.{title}"}


async def test_add_mcp_entries_appends_to_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"mcp": [_mcp_entry("a")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.add_mcp_entries([_mcp_entry("b")])

    assert [e["title"] for e in store.manifest["mcp"]] == ["a", "b"]


async def test_add_mcp_entries_from_missing_section(monkeypatch: pytest.MonkeyPatch) -> None:
    # A never-populated ``mcp`` section (absent key) is treated as an empty list.
    store = _MutateStore(manifest={})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.add_mcp_entries([_mcp_entry("a")])

    assert [e["title"] for e in store.manifest["mcp"]] == ["a"]


async def test_add_mcp_entries_collision_without_replace_400(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"mcp": [_mcp_entry("a"), _mcp_entry("b")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(BadRequestError, match=r"'a'.*'b'|\['a', 'b'\]"):
        await manifest_ops.add_mcp_entries([_mcp_entry("a"), _mcp_entry("b"), _mcp_entry("c")])

    assert store.persisted == []  # refused before any persist


async def test_add_mcp_entries_replace_swaps_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"mcp": [_mcp_entry("a"), _mcp_entry("b")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.add_mcp_entries([_mcp_entry("b", url="https://new/mcp"), _mcp_entry("c")], replace=True)

    # ``b`` swapped at its position, ``c`` appended; ``a`` untouched.
    assert [e["title"] for e in store.manifest["mcp"]] == ["a", "b", "c"]
    assert store.manifest["mcp"][1]["config"]["url"] == "https://new/mcp"


async def test_add_mcp_entries_duplicate_incoming_titles_400(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"mcp": []})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(BadRequestError, match="dup"):
        await manifest_ops.add_mcp_entries([_mcp_entry("dup"), _mcp_entry("dup")])

    assert store.persisted == []


async def test_add_mcp_entries_non_dict_or_titleless_400(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"mcp": []})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(BadRequestError, match="position 0"):
        await manifest_ops.add_mcp_entries(["not-a-dict"])
    with pytest.raises(BadRequestError, match="position 0"):
        await manifest_ops.add_mcp_entries([{"config": {}}])

    assert store.persisted == []


async def test_add_mcp_entries_empty_list_400_nothing_happens(monkeypatch: pytest.MonkeyPatch) -> None:
    # The empty-list refusal precedes ``apply_change`` entirely: no persist, no reload, no
    # broadcast — asserted via the store / admin / bus observables.
    store = _MutateStore(manifest={"mcp": [_mcp_entry("a")]})
    admin = _ReloadAdmin()
    bus = _install_mutate_pipeline(monkeypatch, store=store, admin=admin)

    with pytest.raises(BadRequestError, match="entries must be a non-empty list"):
        await manifest_ops.add_mcp_entries([])

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_add_mcp_entries_malformed_entry_pipeline_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # A titled but structurally invalid entry (no ``config``) passes the title guard and is
    # refused by the manifest pipeline inside the transaction — nothing persisted.
    store = _MutateStore(manifest={"mcp": []})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(BadRequestError, match="invalid mcp config"):
        await manifest_ops.add_mcp_entries([{"title": "x"}])

    assert store.persisted == []


async def test_remove_mcp_entry_removes_named_only(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"mcp": [_mcp_entry("a"), _mcp_entry("b")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.remove_mcp_entry("a")

    assert [e["title"] for e in store.manifest["mcp"]] == ["b"]


async def test_remove_mcp_entry_unknown_404_nothing_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"mcp": [_mcp_entry("a")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(NotFoundError, match="ghost"):
        await manifest_ops.remove_mcp_entry("ghost")

    assert store.persisted == []


async def test_remove_mcp_entry_missing_section_404(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(NotFoundError, match="ghost"):
        await manifest_ops.remove_mcp_entry("ghost")

    assert store.persisted == []


async def test_add_mcp_entries_backend_without_bus_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # Byte-parallel to set_mcp_config: an add whose resolved config needs the bus with none
    # configured raises the RuntimeError ``BackendNeedsBusError`` at MUTATE time; the op maps
    # it to a loud 400 naming TAI_BUS_REDIS_URL rather than letting it escape as a 500.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        store = _MutateStore(manifest={"backend_module": "myapp.backend", "mcp": []})
        _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

        with pytest.raises(BadRequestError, match="TAI_BUS_REDIS_URL"):
            await manifest_ops.add_mcp_entries([_mcp_entry("a")])

        assert store.persisted == []
    finally:
        reset_all_settings()


async def test_remove_mcp_entry_backend_without_bus_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # The remove path validates the whole resulting manifest too, so the same
    # backend-needs-bus invariant maps to a loud 400 (never a 500).
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        store = _MutateStore(manifest={"backend_module": "myapp.backend", "mcp": [_mcp_entry("a")]})
        _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

        with pytest.raises(BadRequestError, match="TAI_BUS_REDIS_URL"):
            await manifest_ops.remove_mcp_entry("a")

        assert store.persisted == []
    finally:
        reset_all_settings()


async def test_remove_mcp_entry_pipeline_400_on_dangling_marker_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    # Removal validates the whole remaining manifest: a dangling ``!ENV`` marker on a
    # DIFFERENT entry is a loud 400 from inside the transaction, nothing persisted.
    monkeypatch.delenv("MISSING_XYZ", raising=False)
    dangling = {
        "title": "b",
        "config": {
            "type": "streamable_http",
            "url": "https://x/mcp",
            "headers": {"Authorization": "!ENV ${MISSING_XYZ}"},
        },
    }
    store = _MutateStore(manifest={"mcp": [_mcp_entry("a"), dangling]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(BadRequestError, match="MISSING_XYZ"):
        await manifest_ops.remove_mcp_entry("a")

    assert store.persisted == []


async def test_add_tools_entries_happy_and_collision_and_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"tools": [_tools_entry("a")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.add_tools_entries([_tools_entry("b")])
    assert [e["title"] for e in store.manifest["tools"]] == ["a", "b"]

    with pytest.raises(BadRequestError, match="a"):
        await manifest_ops.add_tools_entries([_tools_entry("a")])

    await manifest_ops.add_tools_entries([_tools_entry("a", module="pkg.new")], replace=True)
    assert store.manifest["tools"][0]["module"] == "pkg.new"
    assert [e["title"] for e in store.manifest["tools"]] == ["a", "b"]


async def test_remove_tools_entry_happy_and_404(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"tools": [_tools_entry("a"), _tools_entry("b")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.remove_tools_entry("a")
    assert [e["title"] for e in store.manifest["tools"]] == ["b"]

    with pytest.raises(NotFoundError, match="ghost"):
        await manifest_ops.remove_tools_entry("ghost")


async def test_add_agents_entries_happy_and_collision_and_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"agents": [_agents_entry("a")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.add_agents_entries([_agents_entry("b")])
    assert [e["title"] for e in store.manifest["agents"]] == ["a", "b"]

    with pytest.raises(BadRequestError, match="a"):
        await manifest_ops.add_agents_entries([_agents_entry("a")])

    await manifest_ops.add_agents_entries([_agents_entry("a", module="pkg.new")], replace=True)
    assert store.manifest["agents"][0]["module"] == "pkg.new"
    assert [e["title"] for e in store.manifest["agents"]] == ["a", "b"]


async def test_remove_agents_entry_happy_and_404(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"agents": [_agents_entry("a"), _agents_entry("b")]})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.remove_agents_entry("a")
    assert [e["title"] for e in store.manifest["agents"]] == ["b"]

    with pytest.raises(NotFoundError, match="ghost"):
        await manifest_ops.remove_agents_entry("ghost")


async def test_update_api_tools_all_four_empty_400(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={})
    admin = _ReloadAdmin()
    bus = _install_mutate_pipeline(monkeypatch, store=store, admin=admin)

    with pytest.raises(BadRequestError, match="nothing to change"):
        await manifest_ops.update_api_tools()

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_update_api_tools_add_and_remove_both_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"api_tools": {"include": ["keep_in"], "exclude": ["drop_ex"]}})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.update_api_tools(include_add=["new_in"], exclude_add=["new_ex"], exclude_remove=["drop_ex"])

    assert store.manifest["api_tools"]["include"] == ["keep_in", "new_in"]
    assert store.manifest["api_tools"]["exclude"] == ["new_ex"]


async def test_update_api_tools_creates_mapping_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    await manifest_ops.update_api_tools(include_add=["op_a"])

    assert store.manifest["api_tools"]["include"] == ["op_a"]


async def test_update_api_tools_add_already_present_400(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"api_tools": {"include": ["op_a"]}})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(BadRequestError, match="op_a"):
        await manifest_ops.update_api_tools(include_add=["op_a"])

    assert store.persisted == []


async def test_update_api_tools_remove_absent_404(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _MutateStore(manifest={"api_tools": {"include": ["op_a"]}})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(NotFoundError, match="op_ghost"):
        await manifest_ops.update_api_tools(include_remove=["op_ghost"])

    assert store.persisted == []


async def test_update_api_tools_overlap_after_edit_pipeline_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # Adding a name to ``include`` that already sits in ``exclude`` produces an
    # include/exclude overlap the pipeline's ApiToolsConfig validator rejects — a 400
    # from inside the transaction, nothing persisted.
    store = _MutateStore(manifest={"api_tools": {"include": [], "exclude": ["op_x"]}})
    _install_mutate_pipeline(monkeypatch, store=store, admin=_ReloadAdmin())

    with pytest.raises(BadRequestError, match="invalid api_tools config"):
        await manifest_ops.update_api_tools(include_add=["op_x"])

    assert store.persisted == []


def test_entry_ops_are_destructive_and_reload_gated() -> None:
    for op in (
        manifest_ops.add_mcp_entries,
        manifest_ops.remove_mcp_entry,
        manifest_ops.add_tools_entries,
        manifest_ops.remove_tools_entry,
        manifest_ops.add_agents_entries,
        manifest_ops.remove_agents_entry,
        manifest_ops.update_api_tools,
    ):
        meta = operation_metadata_of(op)
        assert meta.destructive is True, meta.name
        assert meta.reload_gated is True, meta.name


def test_update_api_tools_is_authority_changing_tier2() -> None:
    meta = operation_metadata_of(manifest_ops.update_api_tools)
    assert meta.authority_changing is True
    assert is_tier2(meta) is True
    # The per-entry add/remove ops stay module-selection (tier-0), like set_mcp_config.
    assert is_tier2(operation_metadata_of(manifest_ops.add_mcp_entries)) is False


# -- list_failed_mcps -------------------------------


async def test_list_failed_mcps_untargeted_reads_local_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(results={"list_failed_mcps": [{"title": "redis", "status": "unavailable"}]})
    bus = _install(monkeypatch, admin=admin)

    result = await manifest_ops.list_failed_mcps()

    # A query rides the same fan-out shape: this worker's list is its self-entry payload.
    assert bus.publish_calls == [
        (
            {"op": "list_failed_mcps"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=[{"title": "redis", "status": "unavailable"}]),
        )
    ]
    assert result["results"][0]["payload"] == [{"title": "redis", "status": "unavailable"}]


async def test_list_failed_mcps_targeted_to_remote_skips_local(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(results={"list_failed_mcps": []})
    bus = _install(monkeypatch, admin=admin, bus=FakeBus(remotes=["serve-w1"]))

    result = await manifest_ops.list_failed_mcps(["serve-w1"])

    assert admin.calls == []  # self not targeted → no local read
    assert bus.publish_calls == [({"op": "list_failed_mcps"}, ["serve-w1"], None)]
    assert {r["name"] for r in result["results"]} == {"serve-w1"}


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


def test_read_ops_are_not_destructive() -> None:
    for op in (
        manifest_ops.get_manifest,
        manifest_ops.get_mcp_config_schema,
        manifest_ops.get_mcp_status,
        manifest_ops.list_failed_mcps,
    ):
        meta = operation_metadata_of(op)
        assert meta.destructive is False, meta.name
        assert meta.reload_gated is False, meta.name


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
