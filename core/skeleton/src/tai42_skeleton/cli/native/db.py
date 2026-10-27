"""``tai db`` — apply and inspect database migrations.

``migrate`` applies every pending migration across every discovered component (the
skeleton chains plus the chain of every installed plugin that declares one, in the
environment or the plugins prefix, whether or not the manifest loads it, and of every
plugin the persisted manifest loads) through the kit migration runner; ``--plan`` prints what WOULD be applied without
touching the database. ``status`` reports each component's applied / pending /
checksum verdict. Every command names each plugin component that is off (its override
binding unset) and each installed distribution whose packages no installed metadata records
(an editable install served by an import hook); neither changes the exit code.

Each component connects through its bound database's migrator (DDL-privileged)
identity, resolved through the central registry — distinct from the app's runtime
store roles so the migrator can own the schema. A connection failure or an
unconfigured database is a clean, credential-free message and a non-zero exit,
never a raw traceback.
"""

from __future__ import annotations

import asyncio

import typer
from tai42_cli import app_context
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
    MigrationChainDiscovery,
    discover_all_migration_chains,
)
from tai42_skeleton.db.discovery import chain_skip_message, unlocatable_message

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


async def _apply() -> tuple[list[AppliedMigration], MigrationChainDiscovery]:
    """Discover every chain, then apply them in order: the skeleton baseline, ``states``, the plugins.

    Discovery reads installed metadata only, so every chain is resolved before anything is
    applied and a malformed plugin spec refuses before any migration runs. Returns both the
    applied migrations and the discovery, whose ``skipped`` and ``unlocatable`` are named in
    the report.
    """
    discovery = discover_all_migration_chains()
    return await apply_migrations(discovery.entries), discovery


async def _status() -> tuple[list[ComponentStatus], MigrationChainDiscovery]:
    discovery = discover_all_migration_chains()
    statuses = await migration_status(discovery.entries)
    return statuses, discovery


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


def _discovery_records(discovery: MigrationChainDiscovery) -> dict[str, list[dict[str, str]]]:
    """The JSON keys naming every off component and every installed distribution whose packages cannot be enumerated."""
    return {
        "skipped_chains": [{"component": skip.component} for skip in discovery.skipped],
        "unlocatable": [
            {"distribution": item.distribution, "reason": unlocatable_message(item)} for item in discovery.unlocatable
        ],
    }


def _echo_discovery_notes(discovery: MigrationChainDiscovery) -> None:
    """One line per off component, then one per installed distribution whose packages cannot be enumerated."""
    for skip in discovery.skipped:
        typer.echo(chain_skip_message(skip))
    for item in discovery.unlocatable:
        typer.echo(unlocatable_message(item))


def _emit_status(statuses: list[ComponentStatus], discovery: MigrationChainDiscovery, *, json_output: bool) -> None:
    records = _status_records(statuses)
    if json_output:
        print_json({"status": records, **_discovery_records(discovery)})
    else:
        typer.echo(render_table(records, ["component", "applied", "pending", "mismatches", "status"]))
        _echo_discovery_notes(discovery)


def _run(coro):  # type: ignore[no-untyped-def]
    """Run a migration coroutine, mapping the kit's connection and chain faults to clean CLI failures.

    A connection error names the credential-free target; the registry's not-configured
    and half-set-admin-identity errors and the chain-integrity errors surface their own
    actionable messages; all exit non-zero without a traceback. A malformed installed plugin
    spec is a :class:`~tai42_kit.db.MigrationDiscoveryError` (a
    :class:`~tai42_kit.db.MigrationError`, caught below) naming the distribution, so it maps
    here to the same clean credential-free non-zero.
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
    """Report what ``migrate`` WOULD apply, every off component's chain and every unlocatable install, without applying.

    An off component (its override binding unset) is a valid state: it is named, so the plan
    is honest that its chain will not run, and the exit stays zero. An installed distribution
    whose packages cannot be enumerated is named the same way.
    """
    statuses, discovery = _run(_status())
    pending_total = sum(len(status.pending) for status in statuses)
    if json_output:
        _emit_status(statuses, discovery, json_output=True)
    else:
        _emit_status(statuses, discovery, json_output=False)
        typer.echo(
            f"{pending_total} pending migration(s) across {len(statuses)} component(s) — nothing applied (--plan)."
        )


def _emit_applied(*, json_output: bool) -> None:
    """Apply every pending migration, report what ran, then name every off component and unlocatable install.

    An off component (its override binding unset) is a valid state: its chain is not applied,
    the line naming it follows the report, and the exit stays zero. An installed distribution
    whose packages cannot be enumerated is named after it, the exit unchanged.
    """
    applied, discovery = _run(_apply())
    if json_output:
        applied_records = [
            {"component": item.component, "version": item.version, "name": item.name} for item in applied
        ]
        print_json({"applied": applied_records, **_discovery_records(discovery)})
        return
    if applied:
        for item in applied:
            typer.echo(f"Applied {item.component} {item.version:04d}_{item.name}.")
        typer.echo(f"Applied {len(applied)} migration(s).")
    else:
        typer.echo("Schema is up to date — no migrations to apply.")
    _echo_discovery_notes(discovery)


@app.command("migrate")
def migrate_command(
    ctx: typer.Context,
    plan: bool = typer.Option(False, "--plan", help="Show pending migrations without applying them."),
) -> None:
    """Apply every pending migration across all discovered components.

    Discovery covers the skeleton chains plus the chain of every installed plugin that
    declares one, in the environment or the plugins prefix, whether or not the manifest loads
    it, and of every plugin the persisted manifest loads. ``--plan`` lists what would be
    applied and changes nothing. Idempotent: with nothing pending it reports so and exits 0.
    A plugin component whose override binding is unset is off: its chain is not applied, a
    line names it, and the exit stays 0. An installed distribution whose packages no
    installed metadata records (an editable install served by an import hook) is named by a
    line too, the exit unchanged. Loud on a connection failure, an unconfigured connection, a
    rewritten (checksum-mismatched) chain, or a malformed installed plugin spec.
    """
    json_output = app_context(ctx).json_output
    if plan:
        _emit_plan(json_output=json_output)
    else:
        _emit_applied(json_output=json_output)


@app.command("status")
def status_command(ctx: typer.Context) -> None:
    """Report each component's applied / pending / checksum verdict.

    Reports every discovered component: the skeleton chains plus the chain of every
    installed plugin that declares one, in the environment or the plugins prefix, whether or
    not the manifest loads it, and of every plugin the persisted manifest loads. Exits
    non-zero when any component has pending migrations or a checksum mismatch, so it doubles
    as a CI / pre-deploy gate. A plugin component whose override binding is unset is off, a
    valid state: a line names it and it does not fail the gate; an installed distribution
    whose packages cannot be enumerated is named the same way.
    Loud on a connection failure, an unconfigured connection, or a malformed installed
    plugin spec.
    """
    statuses, discovery = _run(_status())
    _emit_status(statuses, discovery, json_output=app_context(ctx).json_output)
    if any(not status.is_up_to_date for status in statuses):
        raise typer.Exit(1)
