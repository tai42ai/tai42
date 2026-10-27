"""The host-side half of ``tai setup --recover`` — re-mint the owner's key on the deployment.

Recovery runs where the deployment runs: it reads the manifest and the stores the server
reads, so it holds the same trust level as reading the boot log. It resolves the deployment's
manifest (``--manifest-path`` or ``TAI_MANIFEST_PATH``), bridges the stored env and validates
it through the one boot-manifest read every serving entrypoint crosses, runs the same preflight a
server boot runs (the plugins prefix, the schema gate), binds the process app onto the
``tai42_app`` handle as a server boot does, imports the manifest's lifecycle modules under the
plugin-compat refusal so the identity/accounts providers register, and then, with the app still bound, re-mints ONE
surviving owner key through :func:`~tai42_skeleton.access_control.setup_recovery.recover_owner_key`.
A schema-gate refusal, an incompatible lifecycle plugin, a recovery refusal or an unreachable
store surfaces as a clean, credential-free message and a non-zero exit rather than a raw
traceback.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import click
import psycopg
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from tai42_contract.app import tai42_app
from tai42_kit.db import MigrationDiscoveryError, SchemaOutOfDateError, component_store_settings

from tai42_skeleton.access_control.setup_recovery import SetupRecoveryError, recover_owner_key
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.settings.cache import manifest_path as default_manifest_path


def _pg_target() -> str:
    """A credential-free description of the skeleton component's bound Postgres for messages."""
    settings = component_store_settings(SKELETON_COMPONENT)
    return f"{settings.pg_host}:{settings.pg_port}/{settings.pg_db}"


@asynccontextmanager
async def _deployment(manifest_path: str | None) -> AsyncIterator[None]:
    """Load the deployment's manifest and lifecycle modules and keep its app bound for the block.

    Stamps the resolved manifest path so the one boot-manifest read finds it, reads and
    validates the manifest under the bridged stored env (the read builds the worker bus and
    reads its census on the running loop, the same loop the re-mint runs on), then runs the
    preflight a server boot runs (:func:`~tai42_skeleton.app.lifecycle.manifest_imports.prepare_manifest_imports`):
    the configured plugin prefix goes on ``sys.path`` so a prefix-installed lifecycle module
    imports, and the schema gate refuses an out-of-date database with the ``tai db migrate``
    fix before any provider reads a table. It then binds the process app onto ``tai42_app``
    and imports each named lifecycle module under the plugin-compat refusal — the side effect
    that registers the identity/accounts providers this command re-mints through. A lifecycle
    module may register on an app facet at import (``tai42_app.webhook_verifiers``, …), so the
    imports run under the binding, and the block that re-mints runs under it too. The binding
    is restored on exit. A schema-gate refusal or an incompatible lifecycle plugin is a
    :class:`click.ClickException` carrying its message; a failing import raises loudly,
    aborting the command.
    """
    resolved = manifest_path or default_manifest_path()
    if resolved is None:
        raise click.BadParameter("A manifest path is required.", param_hint="'--manifest-path'")
    os.environ["TAI_MANIFEST_PATH"] = resolved

    from tai42_skeleton.app.instance import app
    from tai42_skeleton.app.lifecycle.manifest_imports import prepare_manifest_imports
    from tai42_skeleton.marketplace.compat import CorePluginBootError, refuse_incompatible

    manifest = await app.lifecycle.read_boot_manifest()
    with tai42_app.bound(app):
        try:
            installed = await prepare_manifest_imports(manifest)
            for name in manifest.lifecycle_modules:
                refuse_incompatible(name, "lifecycle", installed)
                importlib.import_module(name)
        except (SchemaOutOfDateError, MigrationDiscoveryError, CorePluginBootError) as exc:
            raise click.ClickException(str(exc)) from exc
        yield


def recover(
    setup_token: str, *, key_user: str | None, key_description: str, manifest_path: str | None
) -> dict[str, Any]:
    """Re-mint the owner's key on this deployment and return the result payload.

    The one door of the recovery behaviour: it runs in a process holding the deployment's env,
    so no HTTP route exists. The deployment read and the re-mint run on ONE event loop, so a
    pooled client the boot-manifest read opens is used on the loop that created it. The
    deployment's app stays bound on ``tai42_app`` from the lifecycle-module imports through the
    re-mint. Maps a recovery refusal and a store connection failure to a loud, credential-free
    :class:`click.ClickException` (the root group renders ``Error: …`` and exits non-zero); the
    plaintext key rides in the returned payload, never a log line.
    """
    return asyncio.run(
        _recover(setup_token, key_user=key_user, key_description=key_description, manifest_path=manifest_path)
    )


async def _recover(
    setup_token: str, *, key_user: str | None, key_description: str, manifest_path: str | None
) -> dict[str, Any]:
    """Load the deployment and re-mint the owner's key on the running loop, mapping its failures."""
    async with _deployment(manifest_path):
        try:
            return await recover_owner_key(setup_token, key_user_id=key_user, key_description=key_description)
        except SetupRecoveryError as exc:
            raise click.ClickException(str(exc)) from exc
        except psycopg.OperationalError as exc:
            raise click.ClickException(f"could not connect to Postgres {_pg_target()}: {exc}") from exc
        except (RedisConnectionError, RedisTimeoutError) as exc:
            raise click.ClickException(
                f"could not connect to the access-control Redis (ACCESS_CONTROL_REDIS_URL): {exc}"
            ) from exc
