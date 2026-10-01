"""``tai db`` — apply and inspect database migrations.

``migrate`` applies every pending migration across every discovered component (the
skeleton chains plus every plugin that declares one — the plugins the deployment
LOADS via the manifest, and the marketplace-installed and prefix-installed plugins)
through the kit migration runner; ``--plan`` prints what WOULD be applied without
touching the database. ``status`` reports each component's applied / pending /
checksum verdict.

Each component connects through its bound database's migrator (DDL-privileged)
identity, resolved through the central registry — distinct from the app's runtime
store roles so the migrator can own the schema. A connection failure or an
unconfigured database is a clean, credential-free message and a non-zero exit,
never a raw traceback.
"""

from __future__ import annotations

import asyncio

import typer
from tai42_cli.commands._common import app_context
from tai42_cli.render import print_json, render_table
from tai42_kit.db import (
    AdminIdentityIncompleteError,
    AppliedMigration,
    ComponentStatus,
    DatabaseNotConfiguredError,
    MigrationError,
    apply_migrations,
    component_binding,
    component_migrator_settings,
    migration_status,
)

from tai42_skeleton.db import (
    SKELETON_COMPONENT,
    SkippedChain,
    discover_all_migration_chains,
    discover_plugin_chains,
    skeleton_entry,
)
from tai42_skeleton.db.discovery import chain_skip_message
from tai42_skeleton.states.db import states_entry

app = typer.Typer(
    name="db",
    help="Apply and inspect database migrations.",
    no_args_is_help=True,
)


def _target() -> str:
    """A credential-free description of the skeleton component's bound database for messages.

    The registry name plus host/port/db.
    """
    name = component_binding(SKELETON_COMPONENT)
    settings = component_migrator_settings(SKELETON_COMPONENT)
    return f"database {name!r} at {settings.pg_host}:{settings.pg_port}/{settings.pg_db}"


async def _apply() -> tuple[list[AppliedMigration], list[SkippedChain]]:
    """Apply the two skeleton-owned chains first, then discover and apply the plugin chains.

    Applies the skeleton baseline and the ``states`` record store FIRST. Plugin
    discovery reads skeleton-owned tables (the marketplace install store), so a fresh
    database must receive the skeleton baseline before the plugin chains can even be
    enumerated. Returns both the applied migrations and every declared plugin chain
    that was skipped because its override binding is unset.
    """
    applied = await apply_migrations([skeleton_entry(), states_entry()])
    plugin = await discover_plugin_chains()
    if plugin.entries:
        applied.extend(await apply_migrations(plugin.entries))
    return applied, plugin.skipped


async def _status() -> tuple[list[ComponentStatus], list[SkippedChain]]:
    discovery = await discover_all_migration_chains()
    statuses = await migration_status(discovery.entries)
    return statuses, discovery.skipped


def _status_records(statuses: list[ComponentStatus]) -> list[dict[str, str]]:
    return [
        {
            "component": status.component,
            "applied": str(len(status.applied_versions)),
            "pending": str(len(status.pending)),
            "mismatches": str(len(status.mismatches)),
            "status": "up-to-date" if status.is_up_to_date else "OUT OF DATE",
        }
        for status in statuses
    ]


def _skip_records(skips: list[SkippedChain]) -> list[dict[str, str]]:
    return [{"component": skip.component} for skip in skips]


def _emit_status(statuses: list[ComponentStatus], skips: list[SkippedChain], *, json_output: bool) -> None:
    records = _status_records(statuses)
    if json_output:
        print_json({"status": records, "skipped_chains": _skip_records(skips)})
    else:
        typer.echo(render_table(records, ["component", "applied", "pending", "mismatches", "status"]))
        for skip in skips:
            typer.echo(chain_skip_message(skip))


def _run(coro):  # type: ignore[no-untyped-def]
    """Run a migration coroutine, mapping the kit's connection and chain faults to clean CLI failures.

    A connection error names the credential-free target; the registry's not-configured
    and half-set-admin-identity errors and the chain-integrity errors surface their own
    actionable messages; all exit non-zero without a traceback. A manifest that EXISTS but
    cannot be read, parsed, or validated is wrapped by discovery as a
    :class:`~tai42_kit.db.MigrationDiscoveryError` (a :class:`~tai42_kit.db.MigrationError`,
    caught below) naming the manifest, so it maps here to the same clean credential-free
    non-zero. An ABSENT manifest is NOT mapped here: discovery treats migrate/status as
    manifest-optional and contributes no manifest chains when the file does not exist, so no
    ``FileNotFoundError`` reaches this seam for that case — leaving ``FileNotFoundError``
    unmapped keeps a genuinely different missing-file fault loud rather than masked as a
    clean exit.
    """
    import psycopg

    try:
        return asyncio.run(coro)
    except psycopg.OperationalError as exc:
        typer.echo(f"Error: could not connect to Postgres {_target()}: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (
        DatabaseNotConfiguredError,
        AdminIdentityIncompleteError,
        ValueError,
        MigrationError,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _emit_plan(*, json_output: bool) -> None:
    """Report what ``migrate`` WOULD apply, and every skipped chain, without applying.

    Exits non-zero when a declared chain was skipped so ``--plan`` is honest a chain will not run.
    """
    statuses, skips = _run(_status())
    pending_total = sum(len(status.pending) for status in statuses)
    if json_output:
        _emit_status(statuses, skips, json_output=True)
    else:
        _emit_status(statuses, skips, json_output=False)
        typer.echo(
            f"{pending_total} pending migration(s) across {len(statuses)} component(s) — nothing applied (--plan)."
        )
    if skips:
        raise typer.Exit(1)


def _emit_applied(*, json_output: bool) -> None:
    """Apply every pending migration, report what ran, then name every skipped chain.

    Exits non-zero after reporting when a declared chain was skipped (its override binding is unset).
    """
    applied, skips = _run(_apply())
    if json_output:
        applied_records = [
            {"component": item.component, "version": item.version, "name": item.name} for item in applied
        ]
        print_json({"applied": applied_records, "skipped_chains": _skip_records(skips)})
        if skips:
            raise typer.Exit(1)
        return
    if applied:
        for item in applied:
            typer.echo(f"Applied {item.component} {item.version:04d}_{item.name}.")
        typer.echo(f"Applied {len(applied)} migration(s).")
    elif not skips:
        typer.echo("Schema is up to date — no migrations to apply.")
    if skips:
        for skip in skips:
            typer.echo(chain_skip_message(skip))
        raise typer.Exit(1)


@app.command("migrate")
def migrate_command(
    ctx: typer.Context,
    plan: bool = typer.Option(False, "--plan", help="Show pending migrations without applying them."),
) -> None:
    """Apply every pending migration across all discovered components.

    Discovery covers the skeleton chains plus every plugin that declares one: the plugins
    the deployment LOADS via the manifest, and the marketplace-installed and
    prefix-installed plugins. ``--plan`` lists what would be applied and changes nothing.
    Idempotent: with nothing pending it reports so and exits 0. Exits non-zero, after
    reporting what ran, when a declared migration chain was skipped (its override binding
    is unset). Manifest-optional: an absent manifest contributes no manifest-loaded chains
    and migrate proceeds on the skeleton, store, and prefix chains. Loud on a connection
    failure, an unconfigured connection, a rewritten (checksum-mismatched) chain, or a
    manifest that exists but is malformed or invalid.
    """
    json_output = app_context(ctx).json_output
    if plan:
        _emit_plan(json_output=json_output)
    else:
        _emit_applied(json_output=json_output)


@app.command("status")
def status_command(ctx: typer.Context) -> None:
    """Report each component's applied / pending / checksum verdict.

    Reports every discovered component: the skeleton chains plus every plugin that declares
    one — the plugins the deployment LOADS via the manifest, and the marketplace-installed
    and prefix-installed plugins. Exits non-zero when any component has pending migrations
    or a checksum mismatch, or when a declared migration chain was skipped (its override
    binding is unset), so it doubles as a CI / pre-deploy gate. Manifest-optional: an absent
    manifest contributes no manifest-loaded chains and status reports the skeleton, store,
    and prefix chains. Loud on a connection failure, an unconfigured connection, or a
    manifest that exists but is malformed or invalid.
    """
    statuses, skips = _run(_status())
    _emit_status(statuses, skips, json_output=app_context(ctx).json_output)
    if any(not status.is_up_to_date for status in statuses) or skips:
        raise typer.Exit(1)
