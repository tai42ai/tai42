"""The unified ``tai`` command.

One Typer app exposes the remote command groups — thin clients over a tai42
server's ``/api/*`` routes — plus the CLI-native ``completion`` and ``version``
commands. The console entry point ``app`` is the compiled click group.

Server-side and local commands (``serve``, ``db``, ``doctor``, ``backend`` …)
are contributed by the server package through the ``tai.commands`` entry-point
group: each entry point loads to a ``register(group: click.Group) -> None``
callable that mounts its commands onto the compiled root group. When only this
client is installed those commands are simply absent; the remote surface stands
on its own.

Registration lives here so each remote command module only fills its own Typer
app without editing this file. An extension's ``register`` receives the compiled
click group and may use click's own API on it, plus the two exported helpers:
:func:`inject_json_flag` (give a Typer-backed leaf the trailing ``--json`` form)
and :func:`mount_launcher` (mount a click launcher command under a name). The
seams the command bodies build on are the
:class:`~tai42_cli.context.AppContext` on the Typer context (for a configured
client), :class:`~tai42_cli.client.ApiClient`, and the
:mod:`tai42_cli.render` helpers.
"""

import os
from importlib.metadata import entry_points
from typing import Any, cast

import click
import typer
from dotenv import load_dotenv
from typer.core import TyperGroup, TyperOption
from typer.main import get_command

from tai42_cli import completion, version
from tai42_cli.client import ApiError
from tai42_cli.commands import (
    agents,
    auth,
    backup,
    channels,
    checkpoints,
    config,
    connectors,
    conversations,
    extensions,
    fleet,
    hooks,
    interactions,
    keys,
    manifest,
    mcp,
    notifications,
    obs,
    plugins,
    presets,
    resources,
    roles,
    runs,
    sandbox,
    schedules,
    scopes,
    storage,
    sub_mcp,
    system,
    templates,
    tool_meta,
    tools,
    traces,
)
from tai42_cli.context import AppContext

# The entry-point group a server package publishes to contribute its local and
# runtime commands (``serve``/``db``/``doctor``/``backend`` …) to this CLI.
_EXTENSION_GROUP = "tai.commands"


class TaiCLIGroup(TyperGroup):
    """Root group that bridges standard-``click`` outcomes into Typer's runner.

    Typer bundles its own vendored copy of click, so Typer's runner only
    recognises its vendored exception types. Contributed launcher commands (and
    the native command modules) raise standard-``click`` exceptions, and remote
    commands raise :class:`ApiError`. This override translates all of those into
    Typer's own control-flow exceptions so they render cleanly — a server/usage
    message on stderr with a non-zero exit — instead of a traceback."""

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except ApiError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except click.exceptions.Exit as exc:
            raise typer.Exit(exc.exit_code) from exc
        except click.exceptions.Abort as exc:
            raise typer.Abort() from exc
        except click.ClickException as exc:
            typer.echo(f"Error: {exc.format_message()}", err=True)
            raise typer.Exit(exc.exit_code) from exc


def _apply_json_flag(ctx: click.Context, _param: click.Parameter, value: bool | None) -> bool | None:
    """Merge a per-subcommand ``--json/--no-json`` into the shared context.

    The root callback owns the flag-first form (``tai --json <cmd>``); this lets the
    same flag ride AFTER the subcommand (``tai <cmd> --json``). ``value`` is ``None``
    unless the flag was actually passed, so the root callback's setting stands when
    the trailing flag is absent."""
    if value is not None and isinstance(ctx.obj, AppContext):
        ctx.obj.json_output = value
    return value


def _json_flag_option() -> TyperOption:
    """A fresh trailing ``--json/--no-json`` flag that merges into the context.

    Built as a :class:`TyperOption` so its parse handling matches the Typer context
    the compiled commands run under (a raw ``click.Option`` reads a context attribute
    Typer's vendored context does not carry)."""
    return TyperOption(
        param_decls=["--json/--no-json"],
        is_flag=True,
        default=None,
        expose_value=False,
        callback=_apply_json_flag,
        help="Emit raw JSON instead of human tables.",
    )


def inject_json_flag(command: click.Command) -> None:
    """Give a compiled command the trailing ``--json/--no-json`` form.

    A group recurses into every subcommand so a leaf reached as ``tai <group> <cmd>``
    accepts the flag in its own right (``tai tools list --json``), matching the root
    callback's flag-first form; a single leaf command gets the flag directly. The flag
    is only meaningful once, so groups keep the root form and only leaf commands carry
    the trailing one.

    A group is detected by its ``commands`` mapping rather than by ``isinstance`` —
    Typer's compiled sub-groups are its own vendored ``Group`` type, not a
    :class:`click.Group` subclass. Extensions apply this to their own Typer-backed
    leaves after mounting them."""
    subcommands = getattr(command, "commands", None)
    if subcommands is not None:
        for sub in subcommands.values():
            inject_json_flag(cast(click.Command, sub))
    else:
        command.params.append(cast(click.Parameter, _json_flag_option()))


def mount_launcher(group: click.Group, launcher: click.Command, name: str) -> None:
    """Mount a click launcher command under its ``tai`` subcommand name.

    The command's own ``name`` is the canonical subcommand name now, so it is set
    here (the rich help lists commands by that name, not by the registration key).
    Extensions use this to mount their raw click launcher commands onto the root
    group."""
    launcher.name = name
    group.add_command(launcher)


cli_app = typer.Typer(
    name="tai",
    cls=TaiCLIGroup,
    help="Operate a tai42 server from the terminal.",
    no_args_is_help=True,
    add_completion=False,
)


@cli_app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json/--no-json",
        help="Emit raw JSON instead of human tables.",
    ),
    server: str | None = typer.Option(
        None,
        "--server",
        metavar="URL",
        help="Server base URL. Resolved: this flag -> TAI_SERVER_URL -> config.toml -> local default.",
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help=(
            "Read the API key as one line from stdin. Resolved: this flag -> TAI_API_KEY -> "
            "config.toml -> interactive prompt. There is no --api-key VALUE flag (a value leaks "
            "via ps and shell history)."
        ),
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        metavar="SECONDS",
        help="Read-timeout window in seconds. Resolved: this flag -> TAI_CLI_TIMEOUT_SECONDS -> default.",
    ),
) -> None:
    # Bootstrap a local ``.env`` once for the whole CLI so every subcommand — the
    # remote client and any contributed launcher alike — sees it. ``TAI_CONFIG_MODE``
    # is read straight from the environment (default ``file``), normalized and
    # validated here so this client needs no server settings module. Under ``k8s`` the
    # platform injects env directly and a local ``.env`` must not shadow it, so the
    # load is skipped.
    raw_mode = os.environ.get("TAI_CONFIG_MODE", "file")
    config_mode = raw_mode.strip().lower()
    if config_mode not in ("file", "k8s"):
        raise typer.BadParameter(
            f"Invalid TAI_CONFIG_MODE={raw_mode!r}. Must be one of: file, k8s",
            param_hint="TAI_CONFIG_MODE",
        )
    if config_mode != "k8s":
        load_dotenv()
    ctx.obj = AppContext(
        json_output=json_output,
        server_override=server,
        api_key_stdin=api_key_stdin,
        timeout_override=timeout,
    )


# Remote command groups — thin clients over the ``/api/*`` routes.
_REMOTE_GROUPS: list[tuple[typer.Typer, str]] = [
    (tools.app, "tools"),
    (presets.app, "presets"),
    (agents.app, "agents"),
    (extensions.app, "extensions"),
    (connectors.app, "connectors"),
    (conversations.app, "conversations"),
    (hooks.app, "hooks"),
    (channels.app, "channels"),
    (checkpoints.app, "checkpoints"),
    (notifications.app, "notifications"),
    (storage.app, "storage"),
    (sandbox.app, "sandbox"),
    (resources.app, "resources"),
    (fleet.app, "fleet"),
    (manifest.app, "manifest"),
    (mcp.app, "mcp"),
    (sub_mcp.app, "sub-mcp"),
    (templates.app, "templates"),
    (config.app, "config"),
    (keys.app, "keys"),
    (scopes.app, "scopes"),
    (roles.app, "roles"),
    (runs.app, "runs"),
    (auth.app, "auth"),
    (backup.app, "backup"),
    (schedules.app, "schedules"),
    (obs.app, "obs"),
    (plugins.app, "plugins"),
    (traces.app, "traces"),
    (interactions.app, "interactions"),
    (system.app, "system"),
    (tool_meta.app, "tool-meta"),
]
for group_app, group_name in _REMOTE_GROUPS:
    cli_app.add_typer(group_app, name=group_name)

# CLI-native pieces the remote client owns outright.
cli_app.add_typer(completion.app, name="completion")
cli_app.command(name="version")(version.version)


def _discover_extensions(root: click.Group) -> None:
    """Load every ``tai.commands`` entry point and let it register its commands.

    Each entry point loads to a ``register(group: click.Group) -> None`` callable and
    is invoked with the compiled root group. Entry points are processed in sorted
    entry-point-name order so contributed help order is deterministic. A failing load
    or a failing ``register`` propagates — a broken contribution is never swallowed."""
    discovered = sorted(entry_points(group=_EXTENSION_GROUP), key=lambda ep: ep.name)
    for ep in discovered:
        register = ep.load()
        register(root)


def _build_app() -> click.Group:
    """Compile the Typer app to a click group, give every native leaf the trailing
    ``--json`` form, then let installed extensions contribute their commands."""
    command = cast(click.Group, get_command(cli_app))
    # Inject the trailing ``--json`` form onto the native tree BEFORE extensions run;
    # each extension applies :func:`inject_json_flag` to its own Typer-backed leaves.
    inject_json_flag(command)
    _discover_extensions(command)
    return command


app = _build_app()


if __name__ == "__main__":
    app()
