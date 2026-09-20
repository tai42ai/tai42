"""``tai principals`` — manage the deployment's principals.

Thin wrappers over the admin-only ``/api/auth/principals*`` routes: list the
principals, create a human or service principal, disable or re-enable one, and
delete one. A principal owns keys; deleting it revokes every key it owns.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    compact,
    covers,
    emit_records,
    emit_result,
    seg,
)

app = typer.Typer(
    name="principals",
    help="Manage the deployment's principals.",
    no_args_is_help=True,
)


class PrincipalKind(StrEnum):
    """The kinds of principal the platform recognizes."""

    human = "human"
    service = "service"


@app.command("list")
@covers(("GET", "/api/auth/principals"))
def list_principals(ctx: typer.Context) -> None:
    """List every principal with its kind, display name, and disabled state.

    Example: ``tai principals list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/auth/principals")
    emit_records(ctx_obj, data, route=("GET", "/api/auth/principals"))


@app.command("create")
@covers(("POST", "/api/auth/principals"))
def create_principal(
    ctx: typer.Context,
    kind: Annotated[PrincipalKind, typer.Option("--kind", help="The principal's kind.")],
    display_name: Annotated[str, typer.Option("--display-name", help="How the principal appears in listings.")],
    role: Annotated[
        str, typer.Option("--role", help="The role template the principal's keys inherit (see `tai roles`).")
    ],
    user: Annotated[str | None, typer.Option("--user", help="The principal's id (omitted = server-minted).")] = None,
) -> None:
    """Create a human or service principal under a role.

    Example: ``tai principals create --kind service --display-name 'CI runner' --role editor``
    """
    ctx_obj = app_context(ctx)
    body = compact({"kind": kind.value, "display_name": display_name, "role": role, "user_id": user})
    with ctx_obj.client() as client:
        data = client.post("/api/auth/principals", json=body)
    emit_result(ctx_obj, data)


@app.command("disable")
@covers(("PUT", "/api/auth/principals/{user_id}"))
def disable_principal(ctx: typer.Context, user: Annotated[str, typer.Argument(help="The principal's id.")]) -> None:
    """Disable a principal — its keys stop authenticating until it is re-enabled.

    Example: ``tai principals disable usr-abc123``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.put(f"/api/auth/principals/{seg(user)}", json={"disabled": True})
    emit_result(ctx_obj, data)


@app.command("enable")
@covers(("PUT", "/api/auth/principals/{user_id}"))
def enable_principal(ctx: typer.Context, user: Annotated[str, typer.Argument(help="The principal's id.")]) -> None:
    """Re-enable a disabled principal — its keys authenticate again.

    Example: ``tai principals enable usr-abc123``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.put(f"/api/auth/principals/{seg(user)}", json={"disabled": False})
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("DELETE", "/api/auth/principals/{user_id}"))
def delete_principal(ctx: typer.Context, user: Annotated[str, typer.Argument(help="The principal's id.")]) -> None:
    """Delete a principal — REVOKES every key it owns and drops its policy.

    Deletion is immediate and cannot be undone: each key the principal owns stops
    authenticating on its next request. List the keys with ``tai keys list`` first.

    Example: ``tai principals delete usr-abc123``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/auth/principals/{seg(user)}")
    emit_result(ctx_obj, data)
