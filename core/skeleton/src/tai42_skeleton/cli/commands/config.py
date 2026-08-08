"""``tai config`` — read and update server configuration.

``env`` and ``mode`` and ``settings-schema`` are thin wrappers over the
``/api/config/*`` routes; ``lint`` is OFFLINE (no server) — it validates a manifest
file's shape against the :class:`Manifest` model and checks that every required
registered setting resolves, for CI / pre-deploy gating.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer
from tai42_kit.settings import registered_settings

from tai42_skeleton.app.route_registry import load_api_routes
from tai42_skeleton.cli.commands._common import (
    app_context,
    covers,
    emit_records,
    emit_result,
    validate_manifest_file,
)

app = typer.Typer(
    name="config",
    help="Read and update server configuration.",
    no_args_is_help=True,
)

env_app = typer.Typer(name="env", help="Read and update the stored env overrides.", no_args_is_help=True)
app.add_typer(env_app, name="env")

profile_app = typer.Typer(name="profile", help="Manage versioned settings profiles.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

# ``@``-prefixed profile names are the apply pipeline's own reserved snapshots
# (e.g. ``@previous``); the door refuses a user-created one, and the CLI rejects it
# up front so the operator gets the reason without a round trip.
_RESERVED_PROFILE_PREFIX = "@"


@env_app.command("get")
@covers(("GET", "/api/config/env"))
def get_env(ctx: typer.Context) -> None:
    """Read the stored env config and the operator's secret-key marks.

    Example: ``tai config env get``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/config/env")
    emit_result(ctx_obj, data)


@env_app.command("set")
@covers(("POST", "/api/config/env"))
def set_env(
    ctx: typer.Context,
    assignment: Annotated[list[str], typer.Argument(help="One or more KEY=VALUE env overrides to merge.")],
) -> None:
    """Merge KEY=VALUE env overrides and hot-reload the process config.

    Example: ``tai config env set LOG_LEVEL=debug FEATURE_X=1``
    """
    ctx_obj = app_context(ctx)
    overrides: dict[str, str] = {}
    for pair in assignment:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected KEY=VALUE, got {pair!r}")
        overrides[key] = value
    with ctx_obj.client() as client:
        data = client.post("/api/config/env", json=overrides)
    emit_result(ctx_obj, data)


@app.command("reload")
@covers(("POST", "/api/config/reload"))
def reload_config(
    ctx: typer.Context,
    target: Annotated[
        list[str] | None, typer.Option("--target", help="A worker to restrict the reload to (repeatable).")
    ] = None,
) -> None:
    """Soft-restart this process from its manifest, fanning out to every worker.

    Refreshes env, resets settings caches, and re-initializes from the manifest
    in-process (no pod restart), then propagates to the fleet; ``--target`` restricts
    the fan-out to named workers.

    Example: ``tai config reload``
    """
    ctx_obj = app_context(ctx)
    targets = list(target) if target else None
    with ctx_obj.client() as client:
        data = client.post("/api/config/reload", json={"targets": targets})
    emit_result(ctx_obj, data)


@app.command("mode")
@covers(("GET", "/api/config/mode"))
def config_mode(ctx: typer.Context) -> None:
    """Read the active config backend mode (file / k8s).

    Example: ``tai config mode``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/config/mode")
    emit_result(ctx_obj, data)


@app.command("settings-schema")
@covers(("GET", "/api/config/settings-schema"))
def settings_schema(ctx: typer.Context) -> None:
    """List the registered settings groups with their resolved field values.

    Example: ``tai config settings-schema --json``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/config/settings-schema")
    emit_records(ctx_obj, data, ["name", "module"], items_key="groups")


@app.command("lint")
def lint(
    file: Annotated[
        str | None, typer.Argument(help="Manifest file to validate; defaults to $TAI_MANIFEST_PATH when set.")
    ] = None,
) -> None:
    """Lint config OFFLINE: a manifest file's shape plus required-settings resolution.

    Validates the manifest file against the ``Manifest`` model (a broken manifest
    fails loudly with the model error) and checks that every required registered
    setting resolves from the environment. No server is started.

    Example: ``tai config lint config/manifest.yml``
    """
    manifest_path = file or os.environ.get("TAI_MANIFEST_PATH")
    if manifest_path is not None:
        if not os.path.isfile(manifest_path):
            raise typer.BadParameter(f"manifest file not found: {manifest_path}", param_hint="FILE")
        validate_manifest_file(manifest_path)
        typer.echo(f"Manifest {manifest_path} is valid.", err=True)
    else:
        typer.echo("No manifest file given and TAI_MANIFEST_PATH is unset; skipping manifest shape check.", err=True)

    unresolved = _unresolved_required_settings()
    if unresolved:
        detail = ", ".join(unresolved)
        raise typer.BadParameter(f"required settings do not resolve: {detail}", param_hint="settings")
    typer.echo("All required settings resolve.", err=True)


def _unresolved_required_settings() -> list[str]:
    """Every ``group.field`` whose setting is required but resolves to no value.

    Mirrors the ``/api/config/settings-schema`` surface: the settings modules are
    imported offline (the same offline import primitive the OpenAPI emitter uses),
    then each required field with an ``env_var`` must be present in the process
    environment (which already includes any bootstrapped ``.env``).
    """
    # Importing the API surface offline registers the skeleton's settings groups —
    # the same set the settings-schema route reports — without booting a server.
    load_api_routes()

    unresolved: list[str] = []
    for group in registered_settings():
        for field in group.fields:
            if field.required and field.env_var and field.env_var not in os.environ:
                unresolved.append(f"{group.name}.{field.name} (${field.env_var})")
    return unresolved


# -- settings profiles --------------------------------------------------------
#
# Thin wrappers over ``/api/config/profiles*``. A profile read (``show``) and a
# version read surface REAL env values, mirroring ``env get`` / ``settings-schema``:
# this authed CLI round-trips values through the editor; masking is a display-side
# concern the server never applies on the wire.


@profile_app.command("list")
@covers(("GET", "/api/config/profiles"))
def list_profiles(ctx: typer.Context) -> None:
    """List the settings profiles (reserved ``@`` snapshots excluded).

    Example: ``tai config profile list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/config/profiles")
    emit_records(ctx_obj, data, ["name", "description"])


@profile_app.command("show")
@covers(("GET", "/api/config/profiles/{name}"))
def show_profile(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Show a profile's active body — ``{description, env, secret_keys}`` with real
    env values (this authed door round-trips values; masking is display-side only).

    Example: ``tai config profile show staging``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/config/profiles/{name}")
    emit_result(ctx_obj, data)


@profile_app.command("set")
@covers(("PUT", "/api/config/profiles/{name}"))
def set_profile(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Profile name (a leading '@' is reserved).")],
    assignment: Annotated[
        list[str] | None,
        typer.Argument(help="KEY=VALUE entries forming the profile's WHOLE env band (apply replaces the stored env)."),
    ] = None,
    description: Annotated[str, typer.Option("--description", help="The profile's human description.")] = "",
    secret_key: Annotated[
        list[str] | None,
        typer.Option("--secret-key", help="An env key to mark secret for display masking (repeatable)."),
    ] = None,
) -> None:
    """Create or update a profile (whole-body replace). The env band is the given
    KEY=VALUE entries; ``--secret-key`` marks which keys are secret. A reserved
    ``@``-prefixed name is rejected up front.

    Example: ``tai config profile set staging LOG_LEVEL=debug API_KEY=xyz --secret-key API_KEY``
    """
    ctx_obj = app_context(ctx)
    if name.startswith(_RESERVED_PROFILE_PREFIX):
        raise typer.BadParameter(
            f"a profile name starting with {_RESERVED_PROFILE_PREFIX!r} is reserved", param_hint="NAME"
        )
    env: dict[str, str] = {}
    for pair in assignment or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected KEY=VALUE, got {pair!r}")
        env[key] = value
    body = {"description": description, "env": env, "secret_keys": list(secret_key or [])}
    with ctx_obj.client() as client:
        data = client.put(f"/api/config/profiles/{name}", json=body)
    emit_result(ctx_obj, data)


@profile_app.command("delete")
@covers(("DELETE", "/api/config/profiles/{name}"))
def delete_profile(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Soft-delete a profile, keeping its version history for audit.

    Example: ``tai config profile delete staging``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/config/profiles/{name}")
    emit_result(ctx_obj, data)


@profile_app.command("diff")
@covers(("POST", "/api/config/profiles/{name}/diff"))
def diff_profile(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Preview a profile against the CURRENT stored env — ``{added, removed, changed,
    recycle_keys, refused_keys}`` with real values (a preview, not the apply report).

    Example: ``tai config profile diff staging``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/config/profiles/{name}/diff")
    emit_result(ctx_obj, data)


@profile_app.command("apply")
@covers(("POST", "/api/config/profiles/{name}/apply"))
def apply_profile(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Apply a profile — replace the stored env with its band, reload, and recycle the
    fleet. Prints the ``{hot, recycle, refused, fanout}`` report plus the per-kind
    ``fresh`` list (names + worker identities only, never env values). DESTRUCTIVE: it
    replaces the whole stored env band.

    Example: ``tai config profile apply staging``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/config/profiles/{name}/apply")
    emit_result(ctx_obj, data)


@profile_app.command("versions")
@covers(
    ("GET", "/api/config/profiles/{name}/versions"),
    ("GET", "/api/config/profiles/{name}/versions/{version}"),
)
def profile_versions(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Profile name.")],
    version: Annotated[
        int | None, typer.Option("--version", help="Show this one version's full body instead of the history list.")
    ] = None,
) -> None:
    """List a profile's version history, or — with ``--version`` — show one version's
    full body (real env values; this door is secret-fenced).

    Example: ``tai config profile versions staging`` / ``... versions staging --version 3``
    """
    ctx_obj = app_context(ctx)
    if version is None:
        with ctx_obj.client() as client:
            data = client.get(f"/api/config/profiles/{name}/versions")
        emit_records(ctx_obj, data, ["version", "created_at", "is_current"])
        return
    with ctx_obj.client() as client:
        data = client.get(f"/api/config/profiles/{name}/versions/{version}")
    emit_result(ctx_obj, data)


@profile_app.command("rollback")
@covers(("POST", "/api/config/profiles/{name}/rollback"))
def rollback_profile(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Profile name.")],
    version: Annotated[int, typer.Argument(help="Target version to make active.")],
) -> None:
    """Re-point a profile's active version to ``version`` (a store re-point; the live
    process is realigned by a later apply, not by this).

    Example: ``tai config profile rollback staging 2``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/config/profiles/{name}/rollback", json={"version": version})
    emit_result(ctx_obj, data)
