"""Tool-metadata operations — the folder tree + the per-tool organizational overlay.

The overlay is UNVERSIONED metadata over ANY tool in the namespace (native plugin
tools, presets, flows), keyed by tool name: a ``display_name``, a folder placement,
user ``tags``, a tri-state ``hidden`` override, and informational ``badges``. These
operations are the single
source of truth for the surface; the HTTP routes in ``routers/tool_meta.py`` are
thin adapters over them. Nothing here binds or rebinds a live tool — the overlay is
organizational, not behavioral — so no op is reload-gated and none fans out on the
worker bus.

Two doors carry the sharp rules:

* **Overlay upsert = MERGE-PATCH (RFC-7396).** A field ABSENT from the request is
  kept unchanged; a field PRESENT is written — including present-with-null, which
  CLEARS it (``display_name``/``folder_id``/``hidden`` back to their empty state, and
  ``hidden``'s cleared state is the tri-state NULL "defer to the plugin declaration").
  A present ``tags`` or ``badges`` array REPLACES that whole set (array values are
  atomic). Field presence is detected at the HTTP edge (the extractor sends only the
  keys the caller provided). A full-row replace is deliberately NOT offered — it would force every
  partial editor (a display-name+tags editor, the visibility control) to re-send
  baselines, and one missed field would silently wipe another surface's data.
* **A blank display_name is REFUSED loudly.** Any value empty AFTER ``strip()`` —
  ``""`` and whitespace-only alike — is rejected at the door; an accepted value is
  STORED stripped. The CLEAR operation is present-null, never a blank string. With
  blanks unstorable, the ``display_name ?? name`` fallback renders a real label
  everywhere (CLI and raw-API writers included), never an empty one.

Folder mutations raise loudly: an unknown folder is a 404, a would-be cycle a 400, a
non-empty delete or a sibling-name collision a 409. A folder name is likewise
non-empty after strip and stored stripped.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from tai42_contract.tool_meta import (
    FolderCycleError,
    FolderNameConflictError,
    FolderNotEmptyError,
    FolderNotFoundError,
)
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    operation,
)

# The machine-readable code the tool-meta write OFF refusals carry when the overlay
# store is unconfigured; the message is rendered from the live binding at raise time.
# Hoisted so upsert / folder-create read one way.
_NOT_CONFIGURED_CODE = "tool-meta-not-configured"
_NOT_CONFIGURED_NOUN = "tool-metadata store"

# -- request models (the emitted spec's requestBody schemas) -----------------


class ToolMetaUpsert(BaseModel):
    """A merge-patch overlay edit. Only the fields PRESENT in the request are
    written; a present-null clears (``hidden: null`` writes the tri-state defer
    state); a present ``tags`` array replaces the whole set. Absent fields are
    unchanged — there is no full-row replace."""

    display_name: str | None = None
    folder_id: str | None = None
    tags: list[str] = []
    hidden: bool | None = None
    badges: list[str] = []


class FolderCreate(BaseModel):
    """A folder-creation request — a ``name`` and an optional ``parent_id``
    (``null`` = a root folder)."""

    name: str
    parent_id: str | None = None


class FolderRename(BaseModel):
    """A folder-rename request — the new ``name``."""

    name: str


class FolderMove(BaseModel):
    """A folder-move request — the new ``parent_id`` (``null`` = move to root)."""

    parent_id: str | None = None


# -- display-name / folder-name normalization --------------------------------


def _clean_label(value: str, field: str) -> str:
    """A non-empty label, stored stripped. Any value empty after ``strip()`` is a
    loud 400 — the ``display_name ?? name`` fallback must never render an empty
    label, and a folder must never be nameless."""
    cleaned = value.strip()
    if not cleaned:
        raise BadRequestError(f"{field} must not be blank")
    return cleaned


# -- overlay -----------------------------------------------------------------


@operation(summary="List the tool-metadata overlay", tags=["tool_meta"])
async def list_tool_meta() -> dict[str, Any]:
    """The whole overlay in one read: every folder (the flat tree) plus every
    per-tool row. The UI builds the tree and merges the rows against the live tool
    list client-side."""
    # OFF gate: with no overlay store the honest answer is the empty overlay — the
    # UI degrades perfectly on empty. No store touched.
    if not component_store_configured(SKELETON_COMPONENT):
        return {"folders": [], "meta": []}
    store = instance.app.tool_meta.store
    folders = await store.list_folders()
    rows = await store.list_meta()
    return {
        "folders": [folder.model_dump() for folder in folders],
        "meta": [row.model_dump() for row in rows],
    }


@operation(
    summary="Upsert a tool's overlay",
    tags=["tool_meta"],
    destructive=True,
    errors=[BadRequestError, NotSupportedError],
    request_model=ToolMetaUpsert,
)
async def upsert_tool_meta(tool_name: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge-patch the overlay row for ``tool_name`` (create it if absent). ``patch``
    holds ONLY the fields the caller sent; each is applied over the current row (or
    the empty defaults when none exists), absent fields unchanged. A blank
    ``display_name`` is refused; a present-null clears; a present ``tags`` or
    ``badges`` array replaces that set. An unknown ``folder_id`` is a loud 400."""
    # OFF gate: a write needs the store — refuse with a named, machine-readable
    # reason rather than reaching for an absent Postgres.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    store = instance.app.tool_meta.store
    # Build a SPARSE patch of only the fields the caller sent (present keys), each
    # already normalized here at the door — a blank ``display_name`` is refused
    # loudly before the store is touched. The read-modify-write itself is atomic in
    # the store (``merge_meta`` runs it in one transaction), so two concurrent
    # patches to the same tool serialize instead of losing an update.
    resolved: dict[str, Any] = {}
    if "display_name" in patch:
        raw = patch["display_name"]
        resolved["display_name"] = None if raw is None else _clean_label(raw, "display_name")
    if "folder_id" in patch:
        resolved["folder_id"] = patch["folder_id"]
    if "tags" in patch:
        resolved["tags"] = patch["tags"]
    if "hidden" in patch:
        resolved["hidden"] = patch["hidden"]
    if "badges" in patch:
        resolved["badges"] = patch["badges"]

    try:
        record = await store.merge_meta(tool_name, patch=resolved)
    except FolderNotFoundError as exc:
        raise BadRequestError(f"folder {resolved.get('folder_id')!r} does not exist") from exc
    return record.model_dump()


@operation(summary="Delete a tool's overlay row", tags=["tool_meta"])
async def delete_tool_meta(tool_name: str) -> dict[str, Any]:
    """Drop the overlay row for ``tool_name``. Idempotent — deleting a tool with no
    row is a no-op, not an error (most tools never own one)."""
    # OFF gate: with no store there is no row to drop — the same idempotent success
    # the documented no-op contract already answers, no store touched.
    if not component_store_configured(SKELETON_COMPONENT):
        return {"tool_name": tool_name, "deleted": True}
    await instance.app.tool_meta.store.delete_meta(tool_name)
    return {"tool_name": tool_name, "deleted": True}


# -- folders -----------------------------------------------------------------


@operation(
    summary="Create a folder",
    tags=["tool_meta"],
    destructive=True,
    errors=[BadRequestError, ConflictError, NotSupportedError],
    request_model=FolderCreate,
)
async def create_folder(name: str, parent_id: str | None = None) -> dict[str, Any]:
    """Create a folder under ``parent_id`` (``null`` = a root folder). A blank name
    is a 400, an unknown ``parent_id`` a 400, and a sibling already holding the name
    a 409."""
    # OFF gate: a write needs the store — refuse with a named, machine-readable
    # reason rather than reaching for an absent Postgres.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})
    clean = _clean_label(name, "name")
    try:
        folder = await instance.app.tool_meta.store.create_folder(clean, parent_id)
    except FolderNotFoundError as exc:
        raise BadRequestError(f"parent folder {parent_id!r} does not exist") from exc
    except FolderNameConflictError as exc:
        raise ConflictError(str(exc)) from exc
    return folder.model_dump()


async def resolve_folder_path(path: str) -> str | None:
    """Resolve a ``/``-style folder ``path`` to its leaf ``folder_id`` in the id-keyed
    tool_meta folder tree, creating every missing segment along the way. Each segment is a
    folder ``name`` under the running parent — an existing sibling of that name is reused, a
    missing one created through ``create_folder(name, parent_id)``. Callers store the returned
    leaf id, never a raw path string. Every RAW segment is run through ``_clean_label``, so a
    blank segment (``"a//b"``) or a path that names no real folder (``""`` / ``"///"``) is a
    LOUD authoring error — never a silently dropped segment or a silent no-op."""
    store = instance.app.tool_meta.store
    children: dict[tuple[str | None, str], str] = {
        (folder.parent_id, folder.name): folder.id for folder in await store.list_folders()
    }
    parent_id: str | None = None
    for segment in path.split("/"):
        clean = _clean_label(segment, "folder path segment")
        leaf = children.get((parent_id, clean))
        if leaf is None:
            leaf = (await store.create_folder(clean, parent_id)).id
            children[(parent_id, clean)] = leaf
        parent_id = leaf
    return parent_id


@operation(
    summary="Rename a folder",
    tags=["tool_meta"],
    destructive=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=FolderRename,
)
async def rename_folder(folder_id: str, name: str) -> dict[str, Any]:
    """Rename a folder in place. A blank name is a 400, an unknown folder a 404, and
    a sibling collision a 409."""
    # OFF gate: with no store no folder can exist — a 404 byte-identical to the
    # genuine miss below, so the door is no oracle for the store's absence.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"folder {folder_id!r} not found")
    clean = _clean_label(name, "name")
    try:
        folder = await instance.app.tool_meta.store.rename_folder(folder_id, clean)
    except FolderNotFoundError as exc:
        raise NotFoundError(f"folder {folder_id!r} not found") from exc
    except FolderNameConflictError as exc:
        raise ConflictError(str(exc)) from exc
    return folder.model_dump()


@operation(
    summary="Move a folder",
    tags=["tool_meta"],
    destructive=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=FolderMove,
)
async def move_folder(folder_id: str, parent_id: str | None = None) -> dict[str, Any]:
    """Re-parent a folder (``null`` = move to root). An unknown folder or parent is a
    404, a move that would form a cycle a 400, and a sibling collision at the
    destination a 409."""
    # OFF gate: with no store no folder can exist — a 404 byte-identical to the
    # genuine folder-miss below (``FolderNotFoundError(folder_id)`` renders the same
    # text), so the door is no oracle.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"folder {folder_id!r} not found")
    try:
        folder = await instance.app.tool_meta.store.move_folder(folder_id, parent_id)
    except FolderCycleError as exc:
        raise BadRequestError(str(exc)) from exc
    except FolderNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except FolderNameConflictError as exc:
        raise ConflictError(str(exc)) from exc
    return folder.model_dump()


@operation(
    summary="Delete a folder",
    tags=["tool_meta"],
    errors=[ConflictError, NotFoundError],
)
async def delete_folder(folder_id: str) -> dict[str, Any]:
    """Delete an EMPTY folder. An unknown folder is a 404; a folder still holding
    subfolders or overlay rows is a 409 (empty it first)."""
    # OFF gate: with no store no folder can exist — a 404 byte-identical to the
    # genuine miss below, so the door is no oracle for the store's absence.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"folder {folder_id!r} not found")
    try:
        await instance.app.tool_meta.store.delete_folder(folder_id)
    except FolderNotEmptyError as exc:
        raise ConflictError(str(exc)) from exc
    except FolderNotFoundError as exc:
        raise NotFoundError(f"folder {folder_id!r} not found") from exc
    return {"folder_id": folder_id, "deleted": True}
