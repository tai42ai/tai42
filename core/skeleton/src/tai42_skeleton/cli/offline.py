"""Offline ``tai`` validators that need the server's own models — no server run.

``manifest validate`` and ``config lint`` load a manifest file and run it through
the in-repo :class:`Manifest` model; ``config lint`` additionally checks that every
required registered setting resolves from the environment. These validate the
SERVER's config against the server's own types, so they live here (in the server
package) and are contributed to the ``tai`` command through the local command group
— the standalone client cannot run them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from tai42_kit.settings import registered_settings

from tai42_skeleton.app.route_registry import load_api_routes


def validate_manifest_file(path: str | Path) -> None:
    """Validate a manifest file against the in-repo :class:`Manifest` model, OFFLINE.

    Loads the YAML (expanding ``!ENV`` tags exactly as the runtime read does), refuses
    any required ``!ENV`` marker left dangling OR any oauth connector whose
    client-credential env is unset against the current env (so an offline validation
    catches the cold-boot phantom ``"N/A"`` and the unresolvable connector credential,
    naming each var + json-pointer), then runs ``Manifest.model_validate``, raising a
    usage error carrying the message on any failure. No server, database, or Redis is
    touched.
    """
    from pyaml_env import parse_config
    from pydantic import ValidationError
    from tai42_kit.utils.data import load_manifest

    from tai42_skeleton.config.boundary import refuse_unresolved_env
    from tai42_skeleton.manifest import Manifest

    try:
        with open(path) as fh:
            text = fh.read()
        raw = parse_config(data=text) or {}
    except Exception as exc:
        raise typer.BadParameter(f"could not read manifest YAML: {exc}", param_hint="FILE") from exc
    if not isinstance(raw, dict):
        raise typer.BadParameter("manifest must be a YAML mapping", param_hint="FILE")
    try:
        # Scan the PRESERVED view (markers intact) so a dangling required marker OR an
        # oauth connector's unset client-credential env is named before the expanded
        # projection silently carries an "N/A".
        refuse_unresolved_env(load_manifest(text), os.environ)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="FILE") from exc
    try:
        Manifest.model_validate(raw)
    except ValidationError as exc:
        raise typer.BadParameter(f"invalid manifest: {exc}", param_hint="FILE") from exc


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


def manifest_validate(
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Path to a manifest.yml file.")
    ],
) -> None:
    """Validate a manifest file against the Manifest model, OFFLINE (no server).

    Loads the YAML (expanding ``!ENV`` tags exactly as the runtime read does) and
    runs ``Manifest.model_validate``. A broken manifest exits non-zero with the
    model's validation error; a valid one prints a confirmation.

    Example: ``tai manifest validate config/manifest.yml``
    """
    validate_manifest_file(file)
    typer.echo(f"Manifest {file} is valid.", err=True)


def config_lint(
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
