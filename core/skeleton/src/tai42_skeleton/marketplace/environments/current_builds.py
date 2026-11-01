"""The distributions the current plugin environments hold, read through their ``current`` symlinks without importing.

A process imports from ``sys.path`` and, once
:func:`~tai42_skeleton.marketplace.environments.activate.activate_plugins_dir` ran, the layer's
current build after it: :func:`process_search_path` is that path, for a reader in a process that
never activates the plugins directory (a CLI command). A server package runs from its own virtual
environment, so its distribution is read from there: :func:`current_server_distributions`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from importlib.metadata import Distribution

from packaging.utils import canonicalize_name

from tai42_skeleton.marketplace.environments.layout import current_layer_site_dirs, current_server_dirs, server_dir
from tai42_skeleton.marketplace.environments.server_package import server_build_distribution
from tai42_skeleton.marketplace.environments.settings import configured_plugins_dir


def process_search_path() -> list[str]:
    """Where the process environment's distributions are looked up: ``sys.path``, then the layer's current site dirs.

    The layer goes last, where :func:`~.activate.activate_plugins_dir` puts it, so the image's
    copy of a distribution is the one found first. A layer with no ``current`` build, or an unset
    ``TAI_PLUGINS_DIR``, adds nothing; a site directory already on ``sys.path`` or absent is not
    added.
    """
    path = list(sys.path)
    plugins_dir = configured_plugins_dir()
    if plugins_dir is not None:
        path.extend(
            str(site) for site in current_layer_site_dirs(plugins_dir) if str(site) not in path and site.is_dir()
        )
    return path


def current_server_distributions(packages: Iterable[str] | None = None) -> dict[str, Distribution]:
    """Each server package's distribution from its own ``current`` environment, by canonical name, never imported.

    ``packages`` names the server packages to read; ``None`` reads every server package in use,
    the ones with a ``current`` build (:func:`~.layout.current_server_dirs`). A named package
    with no ``current`` build is left out. Only the package's own distribution is read
    (:func:`~.server_package.server_build_distribution`); the other distributions in its
    environment are the server's runtime. Empty when ``TAI_PLUGINS_DIR`` is unset. A ``current``
    build without its package raises :class:`~tai42_skeleton.marketplace.errors.PluginsDirError`.
    """
    plugins_dir = configured_plugins_dir()
    if plugins_dir is None:
        return {}
    if packages is None:
        environments = current_server_dirs(plugins_dir)
    else:
        environments = [env for env in (server_dir(plugins_dir, p) for p in packages) if env.current.exists()]
    return {
        canonicalize_name(environment.root.name): server_build_distribution(environment.current, environment.root.name)
        for environment in environments
    }
