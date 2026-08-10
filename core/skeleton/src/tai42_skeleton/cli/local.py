"""The server package's contribution to the ``tai`` command.

:func:`register` is the ``tai.commands`` entry point. It receives the compiled
root click group and mounts every command the server adds beyond the remote
client: the local Typer commands (``db``, ``doctor``, ``catalog``, ``openapi``),
the runtime launchers (``serve``, ``backend``, ``metrics``), and the offline
validators (``config lint``, ``manifest validate``) attached onto the client's own
``config``/``manifest`` groups. A missing attach target is a wiring error and
raises loudly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import click
import typer
from typer.main import get_command

from tai42_skeleton.cli import backend, mcp_app, metrics, offline
from tai42_skeleton.cli.native import catalog, db, doctor, openapi


def _attach_offline(root: click.Group, parent_name: str, fn: Callable[..., None], name: str) -> None:
    """Compile the offline command ``fn`` and add it as ``name`` under the client's
    ``parent_name`` group. The offline validators emit human diagnostics and an exit
    code only — no JSON output — so no ``--json`` flag is injected. The parent group
    is created by the client; if it is absent the wiring is broken, so raise."""
    parent = root.commands.get(parent_name)
    if parent is None or getattr(parent, "commands", None) is None:
        raise RuntimeError(f"cannot attach '{name}': the '{parent_name}' command group is not mounted")
    holder = typer.Typer(add_completion=False)
    holder.command(name)(fn)
    compiled = get_command(holder)
    # A single-command Typer app compiles to that command directly; a multi-command
    # one to a group. Either way extract the one leaf.
    raw_leaf = compiled.commands[name] if isinstance(compiled, click.Group) else compiled
    leaf = cast(click.Command, raw_leaf)
    leaf.name = name
    cast(click.Group, parent).add_command(leaf, name)


def register(group: click.Group) -> None:
    """Mount the server's local and runtime commands onto the ``tai`` root group."""
    from tai42_cli.app import inject_json_flag, mount_launcher

    # Local Typer commands: compile them together, give every leaf the trailing
    # ``--json`` form, then move each onto the root group.
    local_app = typer.Typer()
    local_app.add_typer(db.app, name="db")
    local_app.command(name="doctor")(doctor.doctor)
    local_app.command(name="catalog")(catalog.catalog)
    local_app.command(name="openapi")(openapi.openapi)
    compiled = cast(click.Group, get_command(local_app))
    inject_json_flag(compiled)
    for name, command in compiled.commands.items():
        group.add_command(command, name)

    # Runtime launchers — raw click commands, no JSON output of their own.
    mount_launcher(group, mcp_app.cli, "serve")
    mount_launcher(group, backend.main, "backend")
    mount_launcher(group, metrics.main, "metrics")

    # Offline validators attach onto the client's own remote groups.
    _attach_offline(group, "config", offline.config_lint, "lint")
    _attach_offline(group, "manifest", offline.manifest_validate, "validate")
