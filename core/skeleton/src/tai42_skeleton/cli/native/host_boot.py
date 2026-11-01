"""The deployment block a host-side command runs in, and the one renderer of its refusals.

``tai plugins rebuild``, ``tai plugins uninstall --offline`` and ``tai setup --recover`` run on the
deployment host with no server. Each runs inside :func:`deployment`: it first runs the one boot
read every server crosses
(:meth:`~tai42_skeleton.app.facets.lifecycle.LifecycleFacet.read_boot_manifest`), which captures
the deployment env, applies the env store into the process (so a setting kept only in the store, a
database setting included, resolves as it does for a server), builds the worker bus and reads its
census, and reads and validates the manifest. The manifest path resolves as a server's does
(``TAI_MANIFEST_PATH`` or the config directory) unless the command names one. It then binds the
process app onto ``tai42_app`` for the block, as a server binds it, so a module that resolves the
app handle at import or call time reads this deployment's app.

Each of the three runs its coroutine through :func:`run_host_command`, which renders a
refusal as a :class:`click.ClickException` carrying the operator text (the root group prints
``Error: …`` and exits non-zero).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import click
import psycopg
from tai42_contract.app import tai42_app
from tai42_kit.db import (
    AdminIdentityIncompleteError,
    DatabaseNotConfiguredError,
    MigrationDiscoveryError,
    SchemaOutOfDateError,
)

from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.errors import MarketplaceError, PipFailedError
from tai42_skeleton.operations.errors import operator_message


@asynccontextmanager
async def deployment(manifest_path: str | None = None) -> AsyncIterator[Manifest]:
    """Run the boot read on the process app, then yield the validated manifest with the app bound.

    A given ``manifest_path`` is stamped as ``TAI_MANIFEST_PATH`` before the app is built, so
    the boot read reads that manifest. Building the process app builds the deployment's config
    manager; a config provider the deployment's config mode names but this install lacks is a
    :class:`click.ClickException` carrying its install hint, and so is a manifest that does not
    exist, naming its path. The binding is restored on exit.
    """
    if manifest_path is not None:
        os.environ["TAI_MANIFEST_PATH"] = manifest_path

    from tai42_skeleton.app import instance

    try:
        app = instance.app
    except ImportError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        manifest = await app.lifecycle.read_boot_manifest()
    except FileNotFoundError as exc:
        raise click.ClickException(operator_message(exc)) from exc
    with tai42_app.bound(app):
        yield manifest


def run_host_command[T](main: Coroutine[Any, Any, T]) -> T:
    """Run a host-side command's coroutine on one event loop and return its result.

    One loop runs the boot read and the command, so a pooled client the boot read opens is used
    on the loop that created it. A marketplace refusal (the plugins directory's included), an
    unset or incomplete database setting, an unreachable database, and a refusal of the boot
    preflight (a pending schema, an unreadable migrations chain, a plugin the boot refuses, an
    installed distribution's unreadable metadata) is a :class:`click.ClickException` carrying
    the operator text: the error's message, then each note added to it (a pip failure's message
    is followed by pip's report).
    """
    from tai42_skeleton.marketplace.compat import CorePluginBootError
    from tai42_skeleton.marketplace.installed_packages import InstalledMetadataError

    try:
        return asyncio.run(main)
    except PipFailedError as exc:
        raise click.ClickException(operator_message(exc, text=f"{exc}\n{exc.output.rstrip()}")) from exc
    except (
        MarketplaceError,
        DatabaseNotConfiguredError,
        AdminIdentityIncompleteError,
        psycopg.OperationalError,
        SchemaOutOfDateError,
        MigrationDiscoveryError,
        CorePluginBootError,
        InstalledMetadataError,
    ) as exc:
        raise click.ClickException(operator_message(exc)) from exc
