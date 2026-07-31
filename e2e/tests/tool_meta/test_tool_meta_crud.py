"""C7 — the tool-metadata overlay doors over the REAL Postgres overlay store on a
running stack: the folder tree (create / nest / rename / move / delete with the
not-empty and cycle failures), the per-tool merge-patch overlay
(display_name / tags / folder / hidden), and the preset-lifecycle cascade
(delete drops the row, rename re-keys it, a dangling row survives).

The overlay is UNVERSIONED organizational metadata that never rebinds a live tool
and never fans out on the worker bus, so a single-worker read-back off the store
is authoritative — no reload dance is needed."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack


async def _create_folder(api: ApiClient, name: str, parent_id: str | None = None) -> dict:
    return await api.post("/api/tool-meta/folders", json={"name": name, "parent_id": parent_id})


async def _upsert(api: ApiClient, tool_name: str, patch: dict) -> dict:
    return await api.request("PATCH", f"/api/tool-meta/tools/{tool_name}", json=patch)


async def _create_preset(api: ApiClient, name: str) -> dict:
    return await api.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "overlay-cascade echo preset",
            "fixed_kwargs": {"payload": "baked"},
        },
    )


async def test_folder_tree_create_nest_rename_move(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()

    root = await _create_folder(api, uniq("root"))
    assert root["parent_id"] is None
    child = await _create_folder(api, uniq("child"), root["id"])
    assert child["parent_id"] == root["id"]

    # The whole overlay read carries both folders.
    overlay = await api.get("/api/tool-meta")
    folder_ids = {f["id"] for f in overlay["folders"]}
    assert {root["id"], child["id"]} <= folder_ids

    # Rename the root in place.
    new_name = uniq("renamed")
    renamed = await api.post(f"/api/tool-meta/folders/{root['id']}/rename", json={"name": new_name})
    assert renamed == {"id": root["id"], "name": new_name, "parent_id": None}

    # Move the child to the tree root (parent_id = null).
    moved = await api.post(f"/api/tool-meta/folders/{child['id']}/move", json={"parent_id": None})
    assert moved["parent_id"] is None


async def test_folder_delete_not_empty_then_empty(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    parent = await _create_folder(api, uniq("parent"))
    child = await _create_folder(api, uniq("child"), parent["id"])

    # A non-empty folder refuses deletion loudly (409); empty the tree first.
    non_empty = await api.request_raw("DELETE", f"/api/tool-meta/folders/{parent['id']}")
    assert non_empty.status_code == 409, non_empty.text

    await api.delete(f"/api/tool-meta/folders/{child['id']}")
    deleted = await api.delete(f"/api/tool-meta/folders/{parent['id']}")
    assert deleted == {"folder_id": parent["id"], "deleted": True}

    # Both are gone from the overlay read.
    overlay = await api.get("/api/tool-meta")
    folder_ids = {f["id"] for f in overlay["folders"]}
    assert parent["id"] not in folder_ids
    assert child["id"] not in folder_ids


async def test_folder_move_cycle_rejected(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    a = await _create_folder(api, uniq("a"))
    b = await _create_folder(api, uniq("b"), a["id"])

    # Re-parenting A under its own descendant B would form a cycle — a loud 400.
    cycle = await api.request_raw("POST", f"/api/tool-meta/folders/{a['id']}/move", json={"parent_id": b["id"]})
    assert cycle.status_code == 400, cycle.text


async def test_folder_sibling_name_collision_and_unknown(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    name = uniq("dup")
    first = await _create_folder(api, name)

    # A sibling reusing the name is a 409.
    dup = await api.request_raw("POST", "/api/tool-meta/folders", json={"name": name, "parent_id": None})
    assert dup.status_code == 409, dup.text

    # An unknown (well-formed but absent) folder id is a clean 404 on rename/move/delete.
    ghost = str(uuid.uuid4())
    for method, path, body in (
        ("POST", f"/api/tool-meta/folders/{ghost}/rename", {"name": uniq("x")}),
        ("POST", f"/api/tool-meta/folders/{ghost}/move", {"parent_id": None}),
        ("DELETE", f"/api/tool-meta/folders/{ghost}", None),
    ):
        resp = await api.request_raw(method, path, json=body)
        assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}: {resp.text}"

    # Cleanup so the module's shared stack stays tidy for the read assertions above.
    await api.delete(f"/api/tool-meta/folders/{first['id']}")


async def test_overlay_merge_patch(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    folder = await _create_folder(api, uniq("home"))

    # A full write of every field.
    row = await _upsert(
        api,
        "e2e_echo",
        {"display_name": "Echo Probe", "tags": ["probe", "core"], "folder_id": folder["id"], "hidden": True},
    )
    assert row["tool_name"] == "e2e_echo"
    assert row["display_name"] == "Echo Probe"
    assert sorted(row["tags"]) == ["core", "probe"]
    assert row["folder_id"] == folder["id"]
    assert row["hidden"] is True

    # Merge-patch a single field: only ``tags`` is sent, the rest is untouched.
    patched = await _upsert(api, "e2e_echo", {"tags": ["solo"]})
    assert patched["tags"] == ["solo"]
    assert patched["display_name"] == "Echo Probe"
    assert patched["folder_id"] == folder["id"]
    assert patched["hidden"] is True

    # A present-null clears display_name and defers hidden to the tri-state default.
    cleared = await _upsert(api, "e2e_echo", {"display_name": None, "hidden": None})
    assert cleared["display_name"] is None
    assert cleared["hidden"] is None

    # A blank display_name is refused loudly (whitespace-only strips to empty).
    blank = await api.request_raw("PATCH", "/api/tool-meta/tools/e2e_echo", json={"display_name": "   "})
    assert blank.status_code == 400, blank.text

    # An unknown folder is a loud 400 on the write.
    bad_folder = await api.request_raw("PATCH", "/api/tool-meta/tools/e2e_echo", json={"folder_id": str(uuid.uuid4())})
    assert bad_folder.status_code == 400, bad_folder.text

    # An empty patch (no field present) is a 400.
    empty = await api.request_raw("PATCH", "/api/tool-meta/tools/e2e_echo", json={})
    assert empty.status_code == 400, empty.text

    # Delete the row (idempotent) and empty the folder so the module stays clean.
    dropped = await api.delete("/api/tool-meta/tools/e2e_echo")
    assert dropped == {"tool_name": "e2e_echo", "deleted": True}
    # A second delete is a no-op, not an error.
    await api.delete("/api/tool-meta/tools/e2e_echo")
    await api.delete(f"/api/tool-meta/folders/{folder['id']}")


async def test_preset_delete_cascades_overlay_row(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    name = uniq("preset")
    await _create_preset(api, name)
    await _upsert(api, name, {"tags": ["keep"], "display_name": "Kept"})

    # The overlay row exists keyed by the preset name.
    meta = {row["tool_name"]: row for row in (await api.get("/api/tool-meta"))["meta"]}
    assert name in meta

    # Deleting the preset cascades its overlay row away.
    await api.delete(f"/api/presets/{name}")
    meta_after = {row["tool_name"] for row in (await api.get("/api/tool-meta"))["meta"]}
    assert name not in meta_after


async def test_preset_rename_rekeys_overlay_row(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    name = uniq("preset")
    new_name = uniq("renamed")
    await _create_preset(api, name)
    await _upsert(api, name, {"tags": ["migrated"], "display_name": "Before"})

    await api.post(f"/api/presets/{name}/rename", json={"new_name": new_name})

    meta = {row["tool_name"]: row for row in (await api.get("/api/tool-meta"))["meta"]}
    assert name not in meta, "the old key must be gone after a rename"
    assert new_name in meta, "the overlay row must follow the preset to its new name"
    assert meta[new_name]["tags"] == ["migrated"]
    assert meta[new_name]["display_name"] == "Before"

    # Cleanup: deleting the renamed preset drops the re-keyed row.
    await api.delete(f"/api/presets/{new_name}")


async def test_dangling_overlay_row_survives(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    # A row keyed by a tool name that is NOT a live tool (the plugin-uninstall shape:
    # R5 keeps the row so it returns on reinstall; the UI hides the dangling tool).
    ghost = uniq("uninstalled_tool")
    await _upsert(api, ghost, {"tags": ["orphan"], "display_name": "Orphan"})

    meta = {row["tool_name"]: row for row in (await api.get("/api/tool-meta"))["meta"]}
    assert ghost in meta, "a dangling overlay row must be kept, not swept"
    assert meta[ghost]["tags"] == ["orphan"]

    # It is only removed by an explicit delete.
    await api.delete(f"/api/tool-meta/tools/{ghost}")
    meta_after = {row["tool_name"] for row in (await api.get("/api/tool-meta"))["meta"]}
    assert ghost not in meta_after
