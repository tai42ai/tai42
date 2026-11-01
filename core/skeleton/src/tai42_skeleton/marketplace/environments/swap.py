"""Which build of an environment is in use: the ``current`` swap, the swap back, and pruning.

``current`` is a relative symlink, ``builds/<build id>``, replaced atomically
(``os.symlink`` to ``current.new``, then ``os.replace``), so a reader resolving it sees the
old build or the new one, never nothing.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tai42_skeleton.marketplace.environments.layout import EnvironmentDir
from tai42_skeleton.marketplace.errors import PluginsDirError

_NEXT = "current.new"


def current_build(environment: EnvironmentDir) -> str | None:
    """The build id ``current`` names, or ``None`` when the environment has no ``current``.

    A ``current`` that is not a symlink into ``builds/`` raises :class:`PluginsDirError`.
    """
    link = environment.current
    if not link.is_symlink():
        if link.exists():
            raise PluginsDirError(f"{link} is not a symlink to a build")
        return None
    target = Path(os.readlink(link))
    if target.parent != Path("builds") or not target.name:
        raise PluginsDirError(f"{link} points at {target}, not at a build under builds/")
    return target.name


def point_current(environment: EnvironmentDir, build_id: str) -> None:
    """Make build ``build_id`` the environment's ``current``, atomically."""
    staged = environment.root / _NEXT
    if staged.is_symlink() or staged.exists():
        staged.unlink()
    os.symlink(Path("builds") / build_id, staged)
    os.replace(staged, environment.current)


def swap_back(environment: EnvironmentDir, previous: str | None, failed: str) -> None:
    """Put ``current`` back on build ``previous`` (or remove it when there was none), then delete build ``failed``."""
    if previous is None:
        environment.current.unlink(missing_ok=True)
    else:
        point_current(environment, previous)
    delete_build(environment, failed)


def delete_build(environment: EnvironmentDir, build_id: str) -> None:
    """Delete build ``build_id``, which ``current`` does not name."""
    shutil.rmtree(environment.build(build_id))


def prune(environment: EnvironmentDir) -> list[Path]:
    """Delete every build other than ``current``'s, leftovers of interrupted builds included; return them."""
    keep = current_build(environment)
    removed: list[Path] = []
    staged = environment.root / _NEXT
    if staged.is_symlink():
        staged.unlink()
    if not environment.builds.is_dir():
        return removed
    for entry in sorted(environment.builds.iterdir()):
        if entry.name == keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed.append(entry)
    return removed


def remove_environment(environment: EnvironmentDir) -> None:
    """Withdraw the environment's ``current``, then delete its whole directory, when it exists.

    ``current`` goes first, so a deletion that fails part-way leaves no ``current`` naming a
    half-deleted build.
    """
    if environment.root.exists():
        environment.current.unlink(missing_ok=True)
        shutil.rmtree(environment.root)
