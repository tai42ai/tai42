"""The image environment: the distributions the serving interpreter provides without the plugin layer.

The layer sits at the end of ``sys.path``, so the image is read as every ``sys.path`` entry
that does not lie under the layer's directory (:func:`image_paths`), the first occurrence of
a distribution name winning, as imports resolve. A layer build pins every image distribution
at its installed version (:func:`environment_constraints`), so pip takes the image's copy as
the only candidate and places no second copy in the layer.
"""

from __future__ import annotations

import importlib.metadata
import site
import sys
import sysconfig
from pathlib import Path

from packaging.utils import canonicalize_name

from tai42_skeleton.marketplace.environments.settings import refuse_environment_inside
from tai42_skeleton.marketplace.errors import PluginsDirError


def canonical_name(metadata: importlib.metadata.PackageMetadata) -> str | None:
    """The canonical name ``metadata`` carries, or ``None`` when it carries no ``Name``.

    Read with ``.get`` rather than the distribution's ``.name`` property, which raises a
    deprecation warning when the header is absent. A distribution with no name cannot be
    named, so it is never the distribution a caller looks for.
    """
    name = metadata.get("Name")
    return None if name is None else canonicalize_name(name)


def reported_version(dist: importlib.metadata.Distribution, described: str, consequence: str) -> str:
    """The ``Version`` a named distribution's metadata reports; one reporting none raises :class:`PluginsDirError`.

    The error reads ``{described} {name!r} reports no version; {consequence}``. The version is
    read with ``.get`` rather than the ``.version`` property, which raises a deprecation
    warning when the header is absent, the case raised here.
    """
    metadata = dist.metadata
    version = metadata.get("Version")
    if not version:
        raise PluginsDirError(f"{described} {metadata['Name']!r} reports no version; {consequence}")
    return version


def environment_site_dirs() -> list[Path]:
    """The resolved site-packages directories the running interpreter imports its environment from.

    The interpreter's own install scheme's ``purelib`` and ``platlib``, every site dir
    :mod:`site` adds for its prefixes (:func:`site.getsitepackages`; in a venv created with
    system site packages these include the base interpreter's), and the user site dir when
    :mod:`site` enables it.
    """
    paths = sysconfig.get_paths()
    directories = [paths["purelib"], paths["platlib"], *site.getsitepackages()]
    if site.ENABLE_USER_SITE:
        directories.append(site.getusersitepackages())
    return [Path(directory).resolve() for directory in directories]


def image_paths(layer_root: Path | None) -> list[str]:
    """The ``sys.path`` entries of the image: every entry not under ``layer_root``.

    Entries are compared by their resolved paths, so the layer's ``current`` symlink and the
    build it names are both the layer. ``None`` (no plugins directory) is every entry. A
    running-environment site dir under ``layer_root`` raises :class:`PluginsDirError`.
    """
    if layer_root is None:
        return list(sys.path)
    refuse_environment_inside(layer_root, environment_site_dirs())
    root = layer_root.resolve()
    return [entry for entry in sys.path if not _under(Path(entry).resolve(), root)]


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def distributions_by_name(paths: list[str]) -> dict[str, importlib.metadata.Distribution]:
    """The named distributions found on ``paths``, by canonical name, the first occurrence winning.

    A distribution whose metadata carries no ``Name`` is passed over, as it names nothing.
    """
    found: dict[str, importlib.metadata.Distribution] = {}
    for dist in importlib.metadata.distributions(path=paths):
        name = canonical_name(dist.metadata)
        if name is not None:
            found.setdefault(name, dist)
    return found


def image_distributions(layer_root: Path | None) -> dict[str, importlib.metadata.Distribution]:
    """The image's named distributions by canonical name, read over :func:`image_paths`."""
    return distributions_by_name(image_paths(layer_root))


def environment_constraints(layer_root: Path | None) -> list[str]:
    """Every image distribution as a sorted ``name==version`` constraint line.

    A named distribution with no version raises :class:`PluginsDirError` naming it, never a
    silent omission that would let the resolver place a second copy in the layer.
    """
    constraints: list[str] = []
    for dist in image_distributions(layer_root).values():
        version = reported_version(dist, "image distribution", "cannot pin it as a plugin layer constraint")
        constraints.append(f"{dist.metadata['Name']}=={version}")
    return sorted(constraints)
