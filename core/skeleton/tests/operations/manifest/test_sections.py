"""Op-level oracles for the per-entry manifest-section edits and ``set_mcp_config``.

The mcp/tools/agents add and remove doors share the title-keyed collision / membership
contract and drive the mutate pipeline (a raise inside the transaction leaves the store
untouched); deep per-entry validation stays with the pipeline. ``set_mcp_config``'s
backend-needs-bus invariant maps to a loud 400. Every entry op is destructive +
reload-gated.
"""

from __future__ import annotations

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.operations import BadRequestError, NotFoundError, operation_metadata_of
from tai42_skeleton.operations import manifest as manifest_ops

from .conftest import _install_mutate_pipeline, _MutateStore, _ReloadAdmin


def _mcp_entry(title: str, url: str = "https://example.com/mcp") -> dict:
    return {"title": title, "config": {"type": "streamable_http", "url": url}}


def _tools_entry(title: str, module: str | None = None) -> dict:
    # The manifest validator refuses two rows sharing a module, so a per-title default
    # keeps distinct entries distinct.
    return {"title": title, "module": module or f"pkg.{title}"}


def _agents_entry(title: str, module: str | None = None) -> dict:
    return {"title": title, "module": module or f"pkg.{title}"}


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
