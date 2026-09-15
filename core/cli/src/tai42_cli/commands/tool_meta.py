"""``tai tool-meta`` — manage the tool-metadata overlay (folders + per-tool rows).

Thin wrappers over the ``/api/tool-meta*`` routes. The overlay is unversioned
organizational metadata over ANY tool (native, preset, flow): a display name, a
folder placement, user tags, a tri-state visibility, and informational capability
badges. The per-tool ``set`` command mirrors the API's MERGE-PATCH: only the flags
you pass are sent, so a field you omit is left unchanged. A CLEAR flag sends the
explicit reset — a present-null (``--clear-display-name`` / ``--clear-folder``) or a
present-empty array (``--clear-tags`` / ``--clear-badges``) — distinct from omitting
the flag entirely.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
    seg,
)

app = typer.Typer(
    name="tool-meta",
    help="Manage the tool-metadata overlay (folders, display names, tags, visibility).",
    no_args_is_help=True,
)

# ``--visibility`` maps a three-value choice onto the tri-state ``hidden`` overlay:
# defer to the plugin declaration, force shown, or force hidden.
_VISIBILITY_TO_HIDDEN: dict[str, bool | None] = {"default": None, "shown": False, "hidden": True}


class _Unset:
    """The absence of a ``--visibility`` flag, distinct from ``"default"`` which maps to a ``None`` ``hidden``."""


_UNSET = _Unset()


def _reject_conflicting_flags(
    display_name: str | None,
    clear_display_name: bool,
    folder: str | None,
    clear_folder: bool,
    tags: list[str] | None,
    clear_tags: bool,
    badges: list[str] | None,
    clear_badges: bool,
) -> None:
    """Raise a usage error when a set flag and its matching clear flag are both given."""
    for set_present, clear_flag, message in (
        (display_name is not None, clear_display_name, "pass either --display-name or --clear-display-name, not both"),
        (folder is not None, clear_folder, "pass either --folder or --clear-folder, not both"),
        (bool(tags), clear_tags, "pass either --tag or --clear-tags, not both"),
        (bool(badges), clear_badges, "pass either --badge or --clear-badges, not both"),
    ):
        if set_present and clear_flag:
            raise typer.BadParameter(message)


def _apply_patch_field(
    body: dict[str, Any], key: str, *, set_present: bool, set_value: Any, clear: bool, clear_value: Any
) -> None:
    """Write one set/clear field pair into the merge-patch body.

    ``body[key]`` becomes ``set_value`` when the field was set, or ``clear_value`` when its clear flag is on;
    otherwise the key is left absent (unchanged).
    """
    if set_present:
        body[key] = set_value
    elif clear:
        body[key] = clear_value


def _visibility_hidden(visibility: str | None) -> bool | _Unset | None:
    """Map ``--visibility`` to its ``hidden`` value, or the unset sentinel when the flag was omitted.

    Raises a usage error on an unrecognised value.
    """
    if visibility is None:
        return _UNSET
    if visibility not in _VISIBILITY_TO_HIDDEN:
        raise typer.BadParameter("--visibility must be one of: default, shown, hidden")
    return _VISIBILITY_TO_HIDDEN[visibility]


def build_overlay_patch(
    display_name: str | None,
    clear_display_name: bool,
    folder: str | None,
    clear_folder: bool,
    tags: list[str] | None,
    clear_tags: bool,
    badges: list[str] | None,
    clear_badges: bool,
    visibility: str | None,
) -> dict[str, Any]:
    """Assemble the merge-patch body from the ``set`` command's flags.

    Combines the set/clear pairs with the visibility choice; only the flags actually passed appear in the result.
    """
    body: dict[str, Any] = {}
    _apply_patch_field(
        body,
        "display_name",
        set_present=display_name is not None,
        set_value=display_name,
        clear=clear_display_name,
        clear_value=None,
    )
    _apply_patch_field(
        body,
        "folder_id",
        set_present=folder is not None,
        set_value=folder,
        clear=clear_folder,
        clear_value=None,
    )
    _apply_patch_field(
        body,
        "tags",
        set_present=bool(tags),
        set_value=list(tags) if tags else [],
        clear=clear_tags,
        clear_value=[],
    )
    _apply_patch_field(
        body,
        "badges",
        set_present=bool(badges),
        set_value=list(badges) if badges else [],
        clear=clear_badges,
        clear_value=[],
    )
    hidden = _visibility_hidden(visibility)
    if not isinstance(hidden, _Unset):
        body["hidden"] = hidden
    return body


@app.command("list")
@covers(("GET", "/api/tool-meta"))
def list_tool_meta(ctx: typer.Context) -> None:
    """Show the whole overlay — every folder and every per-tool row.

    Example: ``tai tool-meta list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/tool-meta")
    emit_result(ctx_obj, data)


@app.command("set")
@covers(("PATCH", "/api/tool-meta/tools/{tool_name}"))
def set_tool_meta(
    ctx: typer.Context,
    tool_name: Annotated[str, typer.Argument(help="The tool whose overlay to edit.")],
    display_name: Annotated[
        str | None, typer.Option("--display-name", help="Set the display label (non-empty).")
    ] = None,
    clear_display_name: Annotated[
        bool, typer.Option("--clear-display-name", help="Clear the display label (defer to the tool name).")
    ] = False,
    folder: Annotated[str | None, typer.Option("--folder", help="Place the tool in this folder id.")] = None,
    clear_folder: Annotated[bool, typer.Option("--clear-folder", help="Remove the tool from its folder.")] = False,
    tags: Annotated[
        list[str] | None, typer.Option("--tag", help="A user tag (repeatable); replaces the whole set when given.")
    ] = None,
    clear_tags: Annotated[bool, typer.Option("--clear-tags", help="Clear all user tags (send an empty set).")] = False,
    visibility: Annotated[
        str | None,
        typer.Option("--visibility", help="Visibility override: default (defer) | shown | hidden."),
    ] = None,
    badges: Annotated[
        list[str] | None,
        typer.Option("--badge", help="A capability badge (repeatable); replaces the whole set when given."),
    ] = None,
    clear_badges: Annotated[
        bool, typer.Option("--clear-badges", help="Clear all capability badges (send an empty set).")
    ] = False,
) -> None:
    """Merge-patch a tool's overlay.

    Only the flags you pass are sent; omit a field to leave it unchanged.

    Example: ``tai tool-meta set web_search --display-name 'Web Search' --tag research``
    """
    ctx_obj = app_context(ctx)
    _reject_conflicting_flags(
        display_name, clear_display_name, folder, clear_folder, tags, clear_tags, badges, clear_badges
    )
    body = build_overlay_patch(
        display_name, clear_display_name, folder, clear_folder, tags, clear_tags, badges, clear_badges, visibility
    )
    if not body:
        raise typer.BadParameter(
            "provide at least one field to set (--display-name/--clear-display-name, "
            "--folder/--clear-folder, --tag/--clear-tags, --badge/--clear-badges, or --visibility)"
        )
    with ctx_obj.client() as client:
        data = client.patch(f"/api/tool-meta/tools/{seg(tool_name)}", json=body)
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("DELETE", "/api/tool-meta/tools/{tool_name}"))
def delete_tool_meta(
    ctx: typer.Context, tool_name: Annotated[str, typer.Argument(help="The tool whose overlay row to drop.")]
) -> None:
    """Delete a tool's whole overlay row (idempotent).

    Example: ``tai tool-meta delete web_search``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/tool-meta/tools/{seg(tool_name)}")
    emit_result(ctx_obj, data)


@app.command("folder-create")
@covers(("POST", "/api/tool-meta/folders"))
def create_folder(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Folder name.")],
    parent: Annotated[str | None, typer.Option("--parent", help="Parent folder id; omit for a root folder.")] = None,
) -> None:
    """Create a folder (root, or under ``--parent``).

    Example: ``tai tool-meta folder-create Research``
    """
    ctx_obj = app_context(ctx)
    body: dict[str, Any] = {"name": name}
    if parent is not None:
        body["parent_id"] = parent
    with ctx_obj.client() as client:
        data = client.post("/api/tool-meta/folders", json=body)
    emit_result(ctx_obj, data)


@app.command("folder-rename")
@covers(("POST", "/api/tool-meta/folders/{folder_id}/rename"))
def rename_folder(
    ctx: typer.Context,
    folder_id: Annotated[str, typer.Argument(help="Folder id.")],
    name: Annotated[str, typer.Argument(help="New folder name.")],
) -> None:
    """Rename a folder.

    Example: ``tai tool-meta folder-rename <id> Archive``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/tool-meta/folders/{seg(folder_id)}/rename", json={"name": name})
    emit_result(ctx_obj, data)


@app.command("folder-move")
@covers(("POST", "/api/tool-meta/folders/{folder_id}/move"))
def move_folder(
    ctx: typer.Context,
    folder_id: Annotated[str, typer.Argument(help="Folder id.")],
    parent: Annotated[
        str | None, typer.Option("--parent", help="New parent folder id; omit to move to the root.")
    ] = None,
) -> None:
    """Re-parent a folder (omit ``--parent`` to move it to the root).

    Example: ``tai tool-meta folder-move <id> --parent <parent-id>``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/tool-meta/folders/{seg(folder_id)}/move", json={"parent_id": parent})
    emit_result(ctx_obj, data)


@app.command("folder-delete")
@covers(("DELETE", "/api/tool-meta/folders/{folder_id}"))
def delete_folder(
    ctx: typer.Context, folder_id: Annotated[str, typer.Argument(help="Folder id (must be empty).")]
) -> None:
    """Delete an empty folder.

    Example: ``tai tool-meta folder-delete <id>``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/tool-meta/folders/{seg(folder_id)}")
    emit_result(ctx_obj, data)
