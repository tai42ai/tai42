"""The host-side half of ``tai setup --recover`` — re-mint the owner's key on the deployment.

Recovery runs where the deployment runs: it reads the manifest and the stores the server
reads, so it holds the same trust level as reading the boot log. It resolves the deployment's
manifest (``--manifest-path`` or ``TAI_MANIFEST_PATH``), bridges the stored env and validates
it through the one boot-manifest read every serving entrypoint crosses, imports the manifest's
lifecycle modules so the identity/accounts providers register, and then re-mints ONE surviving
owner key through :func:`~tai42_skeleton.access_control.setup_recovery.recover_owner_key`. A
recovery refusal or an unreachable store surfaces as a clean, credential-free message and a
non-zero exit rather than a raw traceback.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any

import click
import psycopg
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from tai42_kit.db import component_store_settings

from tai42_skeleton.access_control.setup_recovery import SetupRecoveryError, recover_owner_key
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.settings.cache import manifest_path as default_manifest_path


def _pg_target() -> str:
    """A credential-free description of the skeleton component's bound Postgres for messages."""
    settings = component_store_settings(SKELETON_COMPONENT)
    return f"{settings.pg_host}:{settings.pg_port}/{settings.pg_db}"


def _load_deployment(manifest_path: str | None) -> None:
    """Resolve the manifest and register the deployment's providers, exactly as a server boot does.

    Stamps the resolved manifest path so the one boot-manifest read finds it, reads and
    validates the manifest under the bridged stored env, activates the configured plugin
    prefix so a prefix-installed lifecycle module imports, then imports every lifecycle
    module — the side effect that registers the identity/accounts providers this command
    re-mints through. A failing import raises loudly: a one-shot command has no quarantine.
    """
    resolved = manifest_path or default_manifest_path()
    if resolved is None:
        raise click.BadParameter("A manifest path is required.", param_hint="'--manifest-path'")
    os.environ["TAI_MANIFEST_PATH"] = resolved

    from tai42_skeleton.app.instance import app
    from tai42_skeleton.marketplace.prefix import activate_prefix

    manifest = app.lifecycle.read_boot_manifest()
    # Put the configured plugin prefix on sys.path before importing any manifest module, so a
    # prefix-installed lifecycle module (a marketplace-installed identity/accounts provider)
    # imports here exactly as it does at serving boot. A no-op when no prefix is configured;
    # idempotent. Imported function-locally to keep the app import chain free of the marketplace
    # package.
    activate_prefix()
    for name in manifest.lifecycle_modules:
        importlib.import_module(name)


def recover(
    setup_token: str, *, key_user: str | None, key_description: str, manifest_path: str | None
) -> dict[str, Any]:
    """Re-mint the owner's key on this deployment and return the result payload.

    The one door of the recovery behaviour: it runs in a process holding the deployment's env,
    so no HTTP route exists. Maps a recovery refusal and a store connection failure to a loud,
    credential-free :class:`click.ClickException` (the root group renders ``Error: …`` and exits
    non-zero); the plaintext key rides in the returned payload, never a log line.
    """
    _load_deployment(manifest_path)
    try:
        return asyncio.run(recover_owner_key(setup_token, key_user_id=key_user, key_description=key_description))
    except SetupRecoveryError as exc:
        raise click.ClickException(str(exc)) from exc
    except psycopg.OperationalError as exc:
        raise click.ClickException(f"could not connect to Postgres {_pg_target()}: {exc}") from exc
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise click.ClickException(
            f"could not connect to the access-control Redis (ACCESS_CONTROL_REDIS_URL): {exc}"
        ) from exc
