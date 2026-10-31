"""``tai version`` — show the installed tai42 packages and key CLI dependencies."""

from __future__ import annotations

from importlib.metadata import Distribution, PackageNotFoundError, PathDistribution, distributions
from importlib.metadata import version as package_version

import click
import typer

from tai42_cli.commands._common import app_context
from tai42_cli.render import print_records

# The runtime deps whose versions most affect the CLI's behavior in the field,
# appended after the discovered tai42 distributions.
_EXTRA_PACKAGES = ["typer", "click", "httpx"]


class InstalledMetadataError(click.ClickException):
    """An installed distribution records no ``Name``, so ``tai version`` cannot account for it."""


def _distribution_name(dist: Distribution) -> str:
    """``dist``'s ``Name`` header, which the core-metadata standard requires.

    Read with ``.get``: an absent header through ``[]`` or ``Distribution.name`` takes the
    deprecated implicit-``None`` path. A distribution recording no ``Name`` (no metadata
    file, or one without the header) raises :class:`InstalledMetadataError` naming where its
    metadata lives.
    """
    name = dist.metadata.get("Name")
    if not name:
        location = str(dist._path) if isinstance(dist, PathDistribution) else repr(dist)
        raise InstalledMetadataError(f"the installed distribution metadata at {location} records no Name")
    return name


def _versions() -> list[dict[str, str]]:
    """Every installed distribution whose name starts with ``tai42-`` (sorted), then key CLI deps.

    No hard-coded package list — whatever tai42 packages the environment carries are what
    report. A distribution recording no ``Name`` raises :class:`InstalledMetadataError`.
    """
    names = {_distribution_name(dist) for dist in distributions()}
    tai_packages = sorted(name for name in names if name.startswith("tai42-"))
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
