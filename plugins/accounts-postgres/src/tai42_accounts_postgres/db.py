"""Packaged DDL loader and CLI to apply/inspect the plugin's schema.

python -m tai42_accounts_postgres.db apply    # create the three tables (idempotent)
python -m tai42_accounts_postgres.db tables    # list the tables the DDL declares
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path
from typing import LiteralString, cast

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient

from tai42_accounts_postgres.settings import AccountsPgSettings

_RESOURCES_DIR = Path(__file__).resolve().parent / "sql"

# Parsed from the DDL so the listing never drifts from what ``apply`` creates.
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", re.IGNORECASE)


def load_ddl() -> str:
    """Return the plugin's full DDL (the ``accounts.init.sql`` resource)."""
    return (_RESOURCES_DIR / "accounts.init.sql").read_text(encoding="utf-8")


def declared_tables() -> list[str]:
    """The tables the packaged DDL creates, sorted."""
    return sorted(set(_CREATE_TABLE_RE.findall(load_ddl())))


def _target(settings: AccountsPgSettings) -> str:
    """A credential-free description of the connection target for messages."""
    return f"{settings.pg_host}:{settings.pg_port}/{settings.pg_db}"


async def _apply_schema(settings: AccountsPgSettings) -> None:
    ddl = load_ddl()
    # Multi-statement script run in one transaction: all-or-nothing.
    async with (
        client_ctx(PostgresClient, settings, fresh=True) as pool,
        pool.connection() as conn,
        conn.transaction(),
    ):
        # Cast: the DDL is trusted packaged schema, not user input.
        await conn.execute(cast("LiteralString", ddl))


def _apply_command() -> None:
    settings = AccountsPgSettings()
    asyncio.run(_apply_schema(settings))
    print(f"Applied accounts schema to {_target(settings)}.")


def _tables_command() -> None:
    for table in declared_tables():
        print(table)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tai42_accounts_postgres.db",
        description="Apply and inspect the tai42-accounts-postgres schema.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("apply", help="Apply the packaged DDL to Postgres (idempotent).")
    sub.add_parser("tables", help="List the tables the DDL declares.")
    args = parser.parse_args(argv)

    if args.command == "apply":
        _apply_command()
    elif args.command == "tables":
        _tables_command()


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    main()
