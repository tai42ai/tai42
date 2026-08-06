"""``tai db`` — apply and inspect database migrations.

``migrate`` applies every pending migration across every discovered component (the
skeleton chain plus every installed plugin that declares one) through the kit
migration runner; ``--plan`` prints what WOULD be applied without touching the
database. ``status`` reports each component's applied / pending / checksum verdict.

Each component connects through its bound database's migrator (DDL-privileged)
identity, resolved through the central registry — distinct from the app's runtime
store roles so the migrator can own the schema. A connection failure or an
unconfigured database is a clean, credential-free message and a non-zero exit,
never a raw traceback.
"""

from __future__ import annotations

import asyncio

import typer
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

from tai42_skeleton.cli.commands._common import app_context
from tai42_skeleton.cli.render import print_json, render_table
from tai42_skeleton.db import SKELETON_COMPONENT, all_migration_entries

app = typer.Typer(
    name="db",
    help="Apply and inspect database migrations.",
    no_args_is_help=True,
)


def _target() -> str:
    """A credential-free description of the skeleton component's bound database for
    messages: the registry name plus host/port/db."""
    name = component_binding(SKELETON_COMPONENT)
    settings = component_migrator_settings(SKELETON_COMPONENT)
    return f"database {name!r} at {settings.pg_host}:{settings.pg_port}/{settings.pg_db}"


async def _apply() -> list[AppliedMigration]:
    entries = await all_migration_entries()
    return await apply_migrations(entries)


async def _status() -> list[ComponentStatus]:
    entries = await all_migration_entries()
    return await migration_status(entries)


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


def _emit_status(statuses: list[ComponentStatus], *, json_output: bool) -> None:
    records = _status_records(statuses)
    if json_output:
        print_json(records)
    else:
        typer.echo(render_table(records, ["component", "applied", "pending", "mismatches", "status"]))


def _run(coro):  # type: ignore[no-untyped-def]
    """Run a migration coroutine, mapping the kit's connection and chain faults to
    clean CLI failures. A connection error names the credential-free target; the
    registry's not-configured and half-set-admin-identity errors and the
    chain-integrity errors surface their own actionable messages; all exit non-zero
    without a traceback."""
    import psycopg

    try:
        return asyncio.run(coro)
    except psycopg.OperationalError as exc:
        typer.echo(f"Error: could not connect to Postgres {_target()}: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (DatabaseNotConfiguredError, AdminIdentityIncompleteError, ValueError, MigrationError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("migrate")
def migrate_command(
    ctx: typer.Context,
    plan: bool = typer.Option(False, "--plan", help="Show pending migrations without applying them."),
) -> None:
    """Apply every pending migration across all discovered components.

    ``--plan`` lists what would be applied and changes nothing. Idempotent: with
    nothing pending it reports so and exits 0. Loud on a connection failure, an
    unconfigured connection, or a rewritten (checksum-mismatched) chain.
    """
    json_output = app_context(ctx).json_output
    if plan:
        statuses = _run(_status())
        pending_total = sum(len(status.pending) for status in statuses)
        if json_output:
            _emit_status(statuses, json_output=True)
        else:
            _emit_status(statuses, json_output=False)
            typer.echo(
                f"{pending_total} pending migration(s) across {len(statuses)} component(s) — nothing applied (--plan)."
            )
        return

    applied = _run(_apply())
    if json_output:
        print_json([{"component": item.component, "version": item.version, "name": item.name} for item in applied])
        return
    if not applied:
        typer.echo("Schema is up to date — no migrations to apply.")
        return
    for item in applied:
        typer.echo(f"Applied {item.component} {item.version:04d}_{item.name}.")
    typer.echo(f"Applied {len(applied)} migration(s).")


@app.command("status")
def status_command(ctx: typer.Context) -> None:
    """Report each component's applied / pending / checksum verdict.

    Exits non-zero when any component has pending migrations or a checksum
    mismatch, so it doubles as a CI / pre-deploy gate. Loud on a connection failure
    or an unconfigured connection.
    """
    statuses = _run(_status())
    _emit_status(statuses, json_output=app_context(ctx).json_output)
    if any(not status.is_up_to_date for status in statuses):
        raise typer.Exit(1)
