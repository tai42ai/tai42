"""Op-level oracles for ``update_api_tools`` (the api_tools include/exclude list edit).

The edit merges add/remove over the stored lists order-stably; an add already present is
a 400, a remove absent a 404, and an include/exclude overlap the pipeline rejects is a
400 from inside the transaction. ``update_api_tools`` is authority-changing (tier-2).
"""

from __future__ import annotations

import pytest

from tai42_skeleton.operations import BadRequestError, NotFoundError, operation_metadata_of
from tai42_skeleton.operations import manifest as manifest_ops
from tai42_skeleton.operations.projection import is_tier2

from .conftest import _install_mutate_pipeline, _MutateStore, _ReloadAdmin


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


def test_update_api_tools_is_authority_changing_tier2() -> None:
    meta = operation_metadata_of(manifest_ops.update_api_tools)
    assert meta.authority_changing is True
    assert is_tier2(meta) is True
    # The per-entry add/remove ops stay module-selection (tier-0), like set_mcp_config.
    assert is_tier2(operation_metadata_of(manifest_ops.add_mcp_entries)) is False
