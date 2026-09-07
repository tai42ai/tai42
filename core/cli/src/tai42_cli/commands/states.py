"""``tai states`` — manage the subject-keyed state store (``/api/states*`` and
``/api/state-retention/prune``).

Thin wrappers over the platform state routes: declare and inspect states, mount modules,
read/write/erase/fold a subject's record, page a subject's write audit trail, list a
state's consumers, and prune expired records. A record is addressed by its subject —
``--target-kind``/``--target-name``/``--kind``/``--key`` — and a document/patch/op batch is
read from a JSON ``--data`` string, a ``--file`` path, or stdin.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
    parse_json_object,
    parse_json_value,
)

app = typer.Typer(name="states", help="Manage the subject-keyed state store.", no_args_is_help=True)


_TARGET_KIND = Annotated[
    str, typer.Option("--target-kind", help="The subject's conversation-target kind (agent/tool).")
]
_TARGET_NAME = Annotated[str, typer.Option("--target-name", help="The subject's conversation-target name.")]
_KIND = Annotated[str, typer.Option("--kind", help="The subject kind (e.g. person, thread).")]
_KEY = Annotated[str, typer.Option("--key", help="The subject key within its kind.")]


def _record_path(name: str, target_kind: str, target_name: str, kind: str, key: str) -> str:
    return f"/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"


def _read_document(data: str | None, file: Path | None) -> Any:
    """A JSON document from ``--data``, a ``--file`` path, or stdin (in that order); a
    document must be supplied through exactly one source."""
    sources = [s for s in (data is not None, file is not None) if s]
    if len(sources) > 1:
        raise typer.BadParameter("pass the document through only one of --data / --file")
    if data is not None:
        return parse_json_value(data, param_hint="--data")
    if file is not None:
        return parse_json_value(file.read_text(), param_hint="--file")
    text = sys.stdin.read()
    if not text.strip():
        raise typer.BadParameter("no document on stdin (pass --data, --file, or pipe JSON)")
    return parse_json_value(text, param_hint="stdin")


# -- declarations -------------------------------------------------------------


@app.command("list")
@covers(("GET", "/api/states"))
def list_states(ctx: typer.Context) -> None:
    """List every declared state."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get("/api/states"))


@app.command("get")
@covers(("GET", "/api/states/{name}"))
def get_state(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The state name.")]) -> None:
    """Show a state's declaration, effective schema, mounts and regimes."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(f"/api/states/{name}"))


@app.command("put")
@covers(("PUT", "/api/states/{name}"))
def put_state(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    data: Annotated[str | None, typer.Option("--data", help="The declaration JSON object.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A file holding the declaration JSON.")] = None,
) -> None:
    """Create or re-declare a state from a declaration document."""
    ctx_obj = app_context(ctx)
    body = _read_document(data, file)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.put(f"/api/states/{name}", json=body))


@app.command("delete")
@covers(("DELETE", "/api/states/{name}"))
def delete_state(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The state name.")]) -> None:
    """Delete a state with its records, mounts and aliases."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.delete(f"/api/states/{name}"))


@app.command("stats")
@covers(("GET", "/api/states/{name}/stats"))
def state_stats(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The state name.")]) -> None:
    """Show a state's record counts (total and per subject kind)."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(f"/api/states/{name}/stats"))


@app.command("migrate")
@covers(("POST", "/api/states/{name}/migrate"))
def migrate_state(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    new_schema: Annotated[str, typer.Option("--new-schema", help="The new base schema JSON object.")],
    transform_expr: Annotated[
        str | None, typer.Option("--transform", help="A jq transform for narrowing records.")
    ] = None,
    confirm_drop: Annotated[
        bool, typer.Option("--confirm-drop", help="Confirm dropping fields a narrowing removes.")
    ] = False,
    resolutions: Annotated[
        str | None, typer.Option("--resolutions", help="A JSON array of per-record resolutions.")
    ] = None,
) -> None:
    """Migrate every record to a new schema (a narrowing needs --transform or --confirm-drop)."""
    ctx_obj = app_context(ctx)
    body: dict[str, Any] = {"new_schema": parse_json_object(new_schema, param_hint="--new-schema")}
    if transform_expr is not None:
        body["transform_expr"] = transform_expr
    body["confirm_drop"] = confirm_drop
    if resolutions is not None:
        body["resolutions"] = parse_json_value(resolutions, param_hint="--resolutions")
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.post(f"/api/states/{name}/migrate", json=body))


@app.command("migrate-preview")
@covers(("POST", "/api/states/{name}/migrate/preview"))
def preview_migrate_state(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    new_schema: Annotated[str, typer.Option("--new-schema", help="The candidate base schema JSON object.")],
) -> None:
    """Dry-run a migration: whether it narrows and which records it would drop or resolve."""
    ctx_obj = app_context(ctx)
    body = {"new_schema": parse_json_object(new_schema, param_hint="--new-schema")}
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.post(f"/api/states/{name}/migrate/preview", json=body))


# -- mounts -------------------------------------------------------------------


@app.command("mounts")
@covers(("GET", "/api/states/{name}/mounts"))
def list_state_mounts(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The state name.")]) -> None:
    """List the modules mounted on a state."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(f"/api/states/{name}/mounts"))


@app.command("mount")
@covers(("PUT", "/api/states/{name}/mounts/{module}"))
def mount_state_module(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    module: Annotated[str, typer.Argument(help="The module name.")],
    data: Annotated[
        str | None, typer.Option("--data", help="The mount body JSON (path/parameters/declarations).")
    ] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A file holding the mount body JSON.")] = None,
) -> None:
    """Mount a module on a state at a path with its parameters and declarations."""
    ctx_obj = app_context(ctx)
    body = _read_document(data, file)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.put(f"/api/states/{name}/mounts/{module}", json=body))


@app.command("update-mount")
@covers(("PATCH", "/api/states/{name}/mounts/{module}"))
def update_state_mount(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    module: Annotated[str, typer.Argument(help="The module name.")],
    declarations: Annotated[str, typer.Option("--declarations", help="The mount declaration values JSON object.")],
) -> None:
    """Replace a mount's static declaration values."""
    ctx_obj = app_context(ctx)
    body = {"declarations": parse_json_object(declarations, param_hint="--declarations")}
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.patch(f"/api/states/{name}/mounts/{module}", json=body))


@app.command("unmount")
@covers(("DELETE", "/api/states/{name}/mounts/{module}"))
def unmount_state_module(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    module: Annotated[str, typer.Argument(help="The module name.")],
) -> None:
    """Unmount a module from a state."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.delete(f"/api/states/{name}/mounts/{module}"))


# -- subjects + records -------------------------------------------------------


@app.command("subjects")
@covers(("GET", "/api/states/{name}/subjects"))
def list_state_subjects(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    kind: Annotated[str | None, typer.Option("--kind", help="Restrict to one subject kind.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Page size.")] = None,
    cursor: Annotated[str | None, typer.Option("--cursor", help="A keyset cursor from a prior page.")] = None,
) -> None:
    """Page the subjects holding a record for a state."""
    ctx_obj = app_context(ctx)
    params = {k: v for k, v in {"kind": kind, "limit": limit, "cursor": cursor}.items() if v is not None}
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(f"/api/states/{name}/subjects", params=params or None))


@app.command("search")
@covers(("POST", "/api/states/{name}/records/search"))
def search_state_records(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    filters: Annotated[str, typer.Option("--filters", help="A JSON object the record document must contain.")],
    limit: Annotated[int | None, typer.Option("--limit", help="Page size.")] = None,
    cursor: Annotated[str | None, typer.Option("--cursor", help="A keyset cursor from a prior page.")] = None,
) -> None:
    """Search a state's records by document containment."""
    ctx_obj = app_context(ctx)
    body: dict[str, Any] = {"filters": parse_json_object(filters, param_hint="--filters")}
    if limit is not None:
        body["limit"] = limit
    if cursor is not None:
        body["cursor"] = cursor
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.post(f"/api/states/{name}/records/search", json=body))


@app.command("read")
@covers(("GET", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"))
def read_state_record(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    target_kind: _TARGET_KIND,
    target_name: _TARGET_NAME,
    kind: _KIND,
    key: _KEY,
) -> None:
    """Read a subject's record (null when none exists)."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(_record_path(name, target_kind, target_name, kind, key)))


@app.command("replace")
@covers(("PUT", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"))
def replace_state_record(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    target_kind: _TARGET_KIND,
    target_name: _TARGET_NAME,
    kind: _KIND,
    key: _KEY,
    data: Annotated[str | None, typer.Option("--data", help="The document JSON.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A file holding the document JSON.")] = None,
) -> None:
    """Replace a subject's whole document."""
    ctx_obj = app_context(ctx)
    body = _read_document(data, file)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.put(_record_path(name, target_kind, target_name, kind, key), json=body))


@app.command("merge")
@covers(("PATCH", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"))
def merge_state_record(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    target_kind: _TARGET_KIND,
    target_name: _TARGET_NAME,
    kind: _KIND,
    key: _KEY,
    data: Annotated[str | None, typer.Option("--data", help="The patch JSON.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A file holding the patch JSON.")] = None,
) -> None:
    """Shallow-merge a patch into a subject's document."""
    ctx_obj = app_context(ctx)
    body = _read_document(data, file)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.patch(_record_path(name, target_kind, target_name, kind, key), json=body))


@app.command("apply")
@covers(("POST", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}/deltas"))
def apply_state_record(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    target_kind: _TARGET_KIND,
    target_name: _TARGET_NAME,
    kind: _KIND,
    key: _KEY,
    data: Annotated[str | None, typer.Option("--data", help="A JSON array of ops (or {ops, op_id}).")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A file holding the ops JSON.")] = None,
    op_id: Annotated[str | None, typer.Option("--op-id", help="An idempotency key for the batch.")] = None,
) -> None:
    """Apply an ordered op batch to a subject's document."""
    ctx_obj = app_context(ctx)
    payload = _read_document(data, file)
    if isinstance(payload, dict) and "ops" in payload:
        body = {"ops": payload["ops"], "op_id": payload.get("op_id", op_id)}
    else:
        body = {"ops": payload, "op_id": op_id}
    with ctx_obj.client() as client:
        emit_result(
            ctx_obj, client.post(f"{_record_path(name, target_kind, target_name, kind, key)}/deltas", json=body)
        )


@app.command("erase")
@covers(("DELETE", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"))
def erase_state_record(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    target_kind: _TARGET_KIND,
    target_name: _TARGET_NAME,
    kind: _KIND,
    key: _KEY,
) -> None:
    """Erase a subject's record."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.delete(_record_path(name, target_kind, target_name, kind, key)))


@app.command("fold")
@covers(("POST", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}/fold"))
def fold_state_record(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    target_kind: _TARGET_KIND,
    target_name: _TARGET_NAME,
    kind: _KIND,
    key: _KEY,
    into: Annotated[str, typer.Option("--into", help="The canonical subject JSON {target_kind,target_name,kind,key}.")],
    mode: Annotated[str, typer.Option("--mode", help="The fold mode.")],
) -> None:
    """Fold this subject's record into another canonical subject."""
    ctx_obj = app_context(ctx)
    body = {"into": parse_json_object(into, param_hint="--into"), "mode": mode}
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.post(f"{_record_path(name, target_kind, target_name, kind, key)}/fold", json=body))


@app.command("writes")
@covers(("GET", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}/writes"))
def list_state_writes(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The state name.")],
    target_kind: _TARGET_KIND,
    target_name: _TARGET_NAME,
    kind: _KIND,
    key: _KEY,
    limit: Annotated[int | None, typer.Option("--limit", help="Page size.")] = None,
    cursor: Annotated[str | None, typer.Option("--cursor", help="A keyset cursor from a prior page.")] = None,
) -> None:
    """Page a subject's write audit trail."""
    ctx_obj = app_context(ctx)
    params = {k: v for k, v in {"limit": limit, "cursor": cursor}.items() if v is not None}
    with ctx_obj.client() as client:
        emit_result(
            ctx_obj,
            client.get(f"{_record_path(name, target_kind, target_name, kind, key)}/writes", params=params or None),
        )


@app.command("consumers")
@covers(("GET", "/api/states/{name}/consumers"))
def state_consumers(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The state name.")]) -> None:
    """List everything that binds a state — flows, hooks, schedules, agents."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(f"/api/states/{name}/consumers"))


@app.command("prune")
@covers(("POST", "/api/state-retention/prune"))
def prune_state_retention(ctx: typer.Context) -> None:
    """Prune every record past its state's retention horizon."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.post("/api/state-retention/prune"))
