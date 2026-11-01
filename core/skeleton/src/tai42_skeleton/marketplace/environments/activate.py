"""Making the current plugin environments visible to this process.

:func:`activate_plugins_dir` runs before every manifest import (every boot and reload). It
declares the kit's stdio launch provider, which returns the console scripts every server
directory's ``current`` build declares for its own package, at each launch, so a server package
installed while this process runs is found without a new declaration. A server directory the
install store does not name holds no ``current`` once an operation dropped it: the uninstall or
update that removes a server package withdraws its ``current`` before the row goes, and every
later operation on a packaged plugin withdraws any other (:mod:`.change`). An install that
stopped between its swap and its row write leaves one, which the next process start refuses
(:mod:`.verify`).

It puts the layer's site directories, through the ``current`` symlink, at the end of
``sys.path`` with :func:`site.addsitedir` (which runs their ``.pth`` files) when the build
``current`` names differs from the one this process last activated; on such a change it first
drops from ``sys.modules`` every module loaded from the layer, so a plugin and its
dependencies re-import together from the new build.
"""

from __future__ import annotations

import importlib
import logging
import os
import site
import sys
from collections.abc import Mapping
from pathlib import Path

from tai42_kit.transport import declare_stdio_launch

from tai42_skeleton.marketplace.environments.console_scripts import server_script_owners
from tai42_skeleton.marketplace.environments.layout import current_layer_site_dirs, current_server_dirs, layer_dir
from tai42_skeleton.marketplace.environments.settings import configured_plugins_dir

logger = logging.getLogger(__name__)


class _Activation:
    """The layer build this process last put on ``sys.path``, by its resolved directory."""

    target: str | None = None


_ACTIVE = _Activation()


def exposed_server_commands() -> Mapping[str, str]:
    """Each console script a server package in use declares, mapped to its ``current/bin`` path.

    The server packages in use are those with a ``current`` build
    (:func:`.layout.current_server_dirs`), and only each package's own scripts count
    (:func:`.console_scripts.server_script_owners`): a build's interpreter and its
    dependencies' scripts are not exposed. A ``current`` without its
    package's distribution, or one script name two server packages declare, raises
    :class:`~tai42_skeleton.marketplace.errors.PluginsDirError`.
    """
    plugins_dir = configured_plugins_dir()
    if plugins_dir is None:
        return {}
    packages = [environment.root.name for environment in current_server_dirs(plugins_dir)]
    return {name: str(owner.path) for name, owner in server_script_owners(plugins_dir, packages).items()}


def activate_plugins_dir() -> None:
    """Declare the server command provider and put the layer's current build on ``sys.path`` when it changed."""
    declare_stdio_launch(exposed_server_commands)
    plugins_dir = configured_plugins_dir()
    if plugins_dir is None:
        return
    environment = layer_dir(plugins_dir)
    target = os.path.realpath(environment.current) if environment.current.exists() else None
    if target == _ACTIVE.target:
        return
    if _ACTIVE.target is not None:
        dropped = drop_layer_modules(environment.root)
        logger.info("plugin layer changed: dropped %d modules loaded from the previous build", dropped)
    for site_dir in current_layer_site_dirs(plugins_dir):
        site.addsitedir(str(site_dir))
    importlib.invalidate_caches()
    _ACTIVE.target = target
    logger.info("plugin layer activated: %s", target)


def _module_paths(module: object) -> list[str]:
    spec = getattr(module, "__spec__", None)
    if spec is None:
        return []
    paths = [spec.origin] if spec.origin else []
    return paths + list(spec.submodule_search_locations or ())


def drop_layer_modules(layer_root: Path) -> int:
    """Remove from ``sys.modules`` every module whose file or package directory lies under ``layer_root``.

    Paths are compared resolved, so a module loaded through the ``current`` symlink and one
    loaded from a build directory are both the layer's. Returns how many were removed.
    """
    root = layer_root.resolve()
    doomed = [
        name
        for name, module in list(sys.modules.items())
        if any(_under(Path(path), root) for path in _module_paths(module))
    ]
    for name in doomed:
        del sys.modules[name]
    return len(doomed)


def _under(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root or root in resolved.parents
