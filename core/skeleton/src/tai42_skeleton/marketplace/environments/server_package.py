"""The distribution a server environment exists for, read from its virtual environment without importing it.

A server package's build is a virtual environment holding the package and its dependencies.
:func:`server_build_distribution` finds the package's own distribution by its normalized name
in the build's site directories; nothing in the build is imported.
"""

from __future__ import annotations

from importlib.metadata import Distribution
from pathlib import Path

from tai42_skeleton.marketplace.environments.layout import server_site_dirs
from tai42_skeleton.marketplace.errors import PluginsDirError


def server_build_distribution(build: Path, package: str) -> Distribution:
    """The distribution ``package`` in the server build at ``build``, found by its normalized name and never imported.

    A build without it raises :class:`PluginsDirError` naming both.
    """
    sites = [str(site) for site in server_site_dirs(build)]
    distribution = next(iter(Distribution.discover(name=package, path=sites)), None)
    if distribution is None:
        raise PluginsDirError(
            f"server build {build} holds no distribution {package}; run 'tai plugins rebuild' against this deployment"
        )
    return distribution
