"""The console scripts of server package builds, and the server package each script name belongs to.

A server package's virtual environment exposes the console scripts its own package declares
and nothing else from its ``bin``: not its interpreter, not its dependencies' scripts; its
distribution is read from the build without importing it (:mod:`.server_package`). One
script name belongs to one server package: :func:`server_script_owners` reads the
``current`` builds of the server packages it is given and refuses a name two of them
declare.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from tai42_skeleton.marketplace.environments.layout import server_dir
from tai42_skeleton.marketplace.environments.server_package import server_build_distribution
from tai42_skeleton.marketplace.errors import PluginsDirError


@dataclass(frozen=True)
class ScriptOwner:
    """The server package that declares a console script, and the script's path in its ``current`` build."""

    package: str
    path: Path


def package_console_scripts(build: Path, package: str) -> dict[str, Path]:
    """Each console script ``package`` declares in the server build at ``build``, mapped to ``build/bin/<name>``.

    A build that holds no distribution ``package`` raises :class:`PluginsDirError` naming both
    (:func:`~.server_package.server_build_distribution`).
    """
    scripts = server_build_distribution(build, package).entry_points.select(group="console_scripts")
    return {script.name: build / "bin" / script.name for script in sorted(scripts, key=lambda entry: entry.name)}


def server_script_owners(plugins_dir: Path, packages: Iterable[str]) -> dict[str, ScriptOwner]:
    """Each console script the ``current`` build of a server package in ``packages`` declares, mapped to its owner.

    ``packages`` names server packages by any spelling; one with no ``current`` build owns no
    script. The path runs through the ``current`` symlink. One script name two server packages
    declare raises :class:`PluginsDirError` naming both.
    """
    owners: dict[str, ScriptOwner] = {}
    for package in sorted({canonicalize_name(package) for package in packages}):
        current = server_dir(plugins_dir, package).current
        if not current.exists():
            continue
        for name, script in package_console_scripts(current, package).items():
            if name in owners:
                raise PluginsDirError(
                    f"server packages {owners[name].package} and {package} both declare the console script "
                    f"{name!r}; a bare mcp command names one package's script — uninstall one of them"
                )
            owners[name] = ScriptOwner(package=package, path=script)
    return owners
