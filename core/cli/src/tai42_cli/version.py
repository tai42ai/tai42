"""``tai version`` — show the installed tai42 packages and key CLI dependencies."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distributions
from importlib.metadata import version as package_version

import typer

from tai42_cli.commands._common import app_context
from tai42_cli.render import print_records

# The runtime deps whose versions most affect the CLI's behavior in the field,
# appended after the discovered tai42 distributions.
_EXTRA_PACKAGES = ["typer", "click", "httpx"]


def _versions() -> list[dict[str, str]]:
    """Every installed distribution whose name starts with ``tai42-`` (sorted),
    followed by the key CLI dependencies. No hard-coded package list — whatever
    tai42 packages the environment carries are what report."""
    tai_packages = sorted({dist.name for dist in distributions() if dist.name.startswith("tai42-")})
    records: list[dict[str, str]] = [{"package": name, "version": package_version(name)} for name in tai_packages]
    for name in _EXTRA_PACKAGES:
        try:
            installed = package_version(name)
        except PackageNotFoundError:
            installed = "not installed"
        records.append({"package": name, "version": installed})
    return records


def version(ctx: typer.Context) -> None:
    """Show the installed tai42 packages and the versions of key CLI dependencies."""
    app_ctx = app_context(ctx)
    print_records(_versions(), ["package", "version"], json_output=app_ctx.json_output)
