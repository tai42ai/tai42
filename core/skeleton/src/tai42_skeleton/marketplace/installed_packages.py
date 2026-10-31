"""Which top-level import packages each installed distribution provides, read without importing anything.

One snapshot of the installed metadata (:class:`InstalledPackages`) serves the
contract-compat verdicts (:mod:`tai42_skeleton.marketplace.compat`) and the discovery of
plugin migration chains (:mod:`tai42_skeleton.db.discovery`), so both map a module to the
same distribution.

A distribution provides the top-level packages its metadata records: the ones its
``top_level.txt`` declares, else the ones its ``RECORD`` lists — the rule
:func:`importlib.metadata.packages_distributions` applies — plus every module importable from
a source root that an editable install puts on ``sys.path`` through a ``.pth`` path line it
records. The ``.pth`` part finds an editable install that writes no ``top_level.txt`` and
records no package file: hatchling, flit, pdm-backend and uv_build record only the ``.pth``
and the ``.dist-info`` files.

An editable install served by an import hook (hatchling ``dev-mode-exact``, pdm-backend
``editables``, meson-python) records none of its packages anywhere: its ``.pth`` holds an
``import`` line that puts a finder on ``sys.meta_path`` when ``site`` processes the site
directory at interpreter start. Such a package is found where the import system serves it
(``importlib.util.find_spec`` on a top-level name runs the finders and never executes the
package) and attributed to the editable distribution whose PEP 610 ``direct_url.json``
source directory holds it. The reverse direction — listing such a distribution's packages —
has no standard source; :func:`hook_served_editable` recognises the distribution so a caller
can name it.

The plugins prefix is put on ``sys.path`` as plain directories, so a ``.pth`` there is never
processed: an editable install in the prefix is served by nothing.
"""

from __future__ import annotations

import csv
import importlib.machinery
import importlib.util
import inspect
import json
import locale
import os
import pkgutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from packaging.utils import canonicalize_name


class InstalledMetadataError(ValueError):
    """An installed distribution's metadata file exists but cannot be read as its standard defines it."""


def recorded_files(dist: metadata.Distribution) -> list[metadata.PackagePath]:
    """The files ``dist`` records as installed, read off its ``RECORD`` rows.

    ``Distribution.files`` checks that every listed file exists, which dominates a scan of
    the whole environment; the rows are read as recorded, so a recorded file that is missing
    on disk fails loudly when it is read. A distribution with no ``RECORD`` (an egg-info
    install) falls to ``Distribution.files``.
    """
    record = dist.read_text("RECORD")
    if record is None:
        return list(dist.files or [])
    return [metadata.PackagePath(row[0]) for row in csv.reader(record.splitlines()) if row]


def _pth_lines(pth: Path) -> list[str]:
    """The lines of the ``.pth`` file ``pth``, decoded as ``site`` decodes them.

    UTF-8, else the locale encoding. A recorded ``.pth`` that is no longer on disk has no
    lines: the interpreter adds nothing from a file it cannot find, and neither does this.
    """
    try:
        content = pth.read_bytes()
    except FileNotFoundError:
        return []
    try:
        return content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return content.decode(locale.getencoding()).splitlines()


def _is_import_line(line: str) -> bool:
    """Whether a ``.pth`` line is one ``site`` executes rather than adds to ``sys.path``."""
    return line.startswith(("import ", "import\t"))


def editable_source_roots(dist: metadata.Distribution, recorded: Sequence[metadata.PackagePath]) -> list[str]:
    """The directories the ``.pth`` files ``dist`` records put on ``sys.path``.

    A ``.pth`` is read the way ``site`` reads one in a site directory: a file whose name
    starts with a dot is not processed; a blank line, a ``#`` comment and an ``import``
    line are not paths; any other line is a directory relative to the site directory, put
    on ``sys.path`` when it exists.
    """
    roots: list[str] = []
    for file in recorded:
        if len(file.parts) != 1 or file.suffix != ".pth" or file.name.startswith("."):
            continue
        pth = Path(str(dist.locate_file(file)))
        for line in _pth_lines(pth):
            if not line.strip() or line.startswith("#") or _is_import_line(line):
                continue
            root = os.path.abspath(os.path.join(pth.parent, line.rstrip()))
            if os.path.isdir(root) and root not in roots:
                roots.append(root)
    return roots


def _recorded_top_level(file: metadata.PackagePath) -> str:
    """The top-level name a recorded file sits under, as ``packages_distributions`` infers it."""
    if len(file.parts) > 1:
        return file.parts[0]
    return inspect.getmodulename(file.name) or file.name


def distribution_top_levels(dist: metadata.Distribution, recorded: Sequence[metadata.PackagePath]) -> list[str]:
    """The top-level import packages ``dist`` provides, in first-seen order.

    ``recorded`` is ``dist``'s :func:`recorded_files`. Its ``top_level.txt`` names when it
    ships one, else the top-level name of every recorded file that is importable (no dot in
    it), then every module importable from its :func:`editable_source_roots`.
    """
    names = (dist.read_text("top_level.txt") or "").split()
    if not names:
        names = [name for name in map(_recorded_top_level, recorded) if "." not in name]
    names.extend(module.name for module in pkgutil.iter_modules(editable_source_roots(dist, recorded)))
    return list(dict.fromkeys(names))


def editable_source_dir(dist: metadata.Distribution) -> Path | None:
    """The project directory ``dist`` was installed from in editable mode, per its PEP 610 ``direct_url.json``.

    ``None`` when the distribution records no ``direct_url.json`` or it describes anything but
    an editable install of a local directory (an archive, a VCS checkout, a non-editable
    directory install). A ``direct_url.json`` that is not a JSON object, or whose
    ``dir_info`` is not an object, is a loud :class:`InstalledMetadataError` naming the
    distribution.
    """
    text = dist.read_text("direct_url.json")
    if text is None:
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstalledMetadataError(
            f"distribution {dist.name!r} records a direct_url.json that is not JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise InstalledMetadataError(f"distribution {dist.name!r} records a direct_url.json that is not a JSON object")
    dir_info = document.get("dir_info")
    if dir_info is None:
        return None
    if not isinstance(dir_info, dict):
        raise InstalledMetadataError(
            f"distribution {dist.name!r} records a direct_url.json whose dir_info is not an object"
        )
    url = urlparse(str(document.get("url", "")))
    if dir_info.get("editable") is not True or url.scheme != "file":
        return None
    return Path(os.path.abspath(url2pathname(url.path)))


def _inside_a_package_directory(file: metadata.PackagePath) -> bool:
    """Whether a recorded file sits under a top-level directory that can be an import package.

    Not a ``.dist-info``/``.data`` directory (a dot in its name) and not the ``__pycache__``
    directory that byte-compiling a top-level module records (PEP 3147).
    """
    return len(file.parts) > 1 and "." not in file.parts[0] and file.parts[0] != "__pycache__"


def hook_served_editable(dist: metadata.Distribution, recorded: Sequence[metadata.PackagePath]) -> bool:
    """Whether ``dist`` is an editable install whose packages its metadata records nowhere.

    ``recorded`` is ``dist``'s :func:`recorded_files`. True for an editable install (PEP 610)
    with no ``top_level.txt``, no recorded file inside a package directory
    (:func:`_inside_a_package_directory`), no ``.pth`` path
    line, and a recorded ``.pth`` that holds an ``import`` line — the shape every import-hook
    editable mode leaves: its packages are served by a finder and listed in no installed file.
    """
    if editable_source_dir(dist) is None or dist.read_text("top_level.txt"):
        return False
    if any(_inside_a_package_directory(file) for file in recorded):
        return False
    if editable_source_roots(dist, recorded):
        return False
    return any(
        _is_import_line(line)
        for file in recorded
        if len(file.parts) == 1 and file.suffix == ".pth" and not file.name.startswith(".")
        for line in _pth_lines(Path(str(dist.locate_file(file))))
    )


def _search_locations(module_spec: importlib.machinery.ModuleSpec) -> list[str]:
    """The filesystem places a top-level module spec points at: its search locations, then its origin's directory.

    A finder may give a package a virtual search location (meson-python's editable loader
    points it inside its own module), so the origin's directory is checked too.
    """
    locations = [str(location) for location in module_spec.submodule_search_locations or []]
    if module_spec.origin and module_spec.has_location:
        locations.append(os.path.dirname(module_spec.origin))
    return [os.path.abspath(location) for location in locations]


@dataclass(frozen=True)
class InstalledDistribution:
    """One installed distribution as a snapshot read it.

    ``dist`` is its metadata handle, ``name`` its required ``Name`` (:func:`distribution_name`),
    ``recorded`` its :func:`recorded_files` and ``top_levels`` the top-level import packages
    it provides (:func:`distribution_top_levels`).
    """

    dist: metadata.Distribution
    name: str
    recorded: Sequence[metadata.PackagePath]
    top_levels: Sequence[str]


def _metadata_location(dist: metadata.Distribution) -> str:
    """Where ``dist``'s metadata lives: its metadata directory, for a distribution read from a path."""
    if isinstance(dist, metadata.PathDistribution):
        return str(dist._path)
    return repr(dist)


def distribution_name(dist: metadata.Distribution) -> str:
    """``dist``'s ``Name`` header, which the core-metadata standard requires.

    Read with ``.get``: an absent header through ``[]`` or ``Distribution.name`` takes the
    deprecated implicit-``None`` path. A distribution recording no ``Name`` (no metadata
    file, or one without the header) is a loud :class:`InstalledMetadataError` naming where
    its metadata lives.
    """
    name = dist.metadata.get("Name")
    if not name:
        raise InstalledMetadataError(
            f"the installed distribution metadata at {_metadata_location(dist)} records no Name"
        )
    return name


def read_distribution(dist: metadata.Distribution) -> InstalledDistribution:
    """``dist``'s name, recorded files and top-level packages, read once from its metadata."""
    name = distribution_name(dist)
    recorded = recorded_files(dist)
    return InstalledDistribution(
        dist=dist, name=name, recorded=recorded, top_levels=distribution_top_levels(dist, recorded)
    )


@dataclass(frozen=True)
class InstalledPackages:
    """One read of the installed distributions: which distribution provides each top-level import package.

    ``top_levels`` maps every top-level name the metadata records to the distributions
    providing it (:func:`distribution_top_levels`). ``editable_sources`` maps each editable
    distribution to its PEP 610 source directory (:func:`editable_source_dir`).
    ``distributions`` holds each distribution the read found, keyed by its canonical name
    (PEP 503), in search-path order; a distribution installed twice is held as its first copy
    on the path, the one the import system serves. :meth:`distributions_for` answers a
    recorded name from ``top_levels`` and any other name through the import system, once per
    name.
    """

    top_levels: Mapping[str, Sequence[str]]
    editable_sources: Mapping[str, Path]
    distributions: Mapping[str, InstalledDistribution]
    _resolved: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False, compare=False)

    def distribution(self, name: str) -> InstalledDistribution:
        """The read of installed distribution ``name``; :class:`importlib.metadata.PackageNotFoundError` when absent."""
        found = self.distributions.get(canonicalize_name(name))
        if found is None:
            raise metadata.PackageNotFoundError(name)
        return found

    def distributions_for(self, top_level: str) -> list[str]:
        """The installed distributions providing top-level import package ``top_level``.

        A name the metadata records maps through ``top_levels``. Any other name is looked up
        with ``importlib.util.find_spec`` — which runs the import finders and never executes
        the package — and attributed to the editable distribution whose source directory
        holds the package; when source directories nest, the innermost one owns it. ``[]``
        when nothing serves the name, or no editable distribution's source holds it (a
        module no installed distribution provides, such as an operator-authored one).
        """
        recorded = self.top_levels.get(top_level)
        if recorded:
            return list(recorded)
        if top_level not in self._resolved:
            self._resolved[top_level] = self._attributed(top_level)
        return list(self._resolved[top_level])

    def _attributed(self, top_level: str) -> list[str]:
        module_spec = importlib.util.find_spec(top_level)
        if module_spec is None:
            return []
        owners: list[tuple[int, str]] = []
        for dist_name, source in self.editable_sources.items():
            if any(Path(location).is_relative_to(source) for location in _search_locations(module_spec)):
                owners.append((len(source.parts), dist_name))
        if not owners:
            return []
        innermost = max(depth for depth, _ in owners)
        return [dist_name for depth, dist_name in owners if depth == innermost]


def installed_packages(path: Sequence[str] | None = None) -> InstalledPackages:
    """The :class:`InstalledPackages` snapshot of the distributions on ``path`` (``sys.path`` when omitted).

    One pass over the distributions: each one's :func:`read_distribution` and, for an
    editable install, its source directory.
    """
    top_levels: dict[str, list[str]] = {}
    editable_sources: dict[str, Path] = {}
    distributions: dict[str, InstalledDistribution] = {}
    for dist in metadata.distributions(path=list(sys.path if path is None else path)):
        read = read_distribution(dist)
        name = read.name
        distributions.setdefault(canonicalize_name(name), read)
        for top_level in read.top_levels:
            top_levels.setdefault(top_level, []).append(name)
        source = editable_source_dir(dist)
        if source is not None:
            editable_sources.setdefault(name, source)
    return InstalledPackages(top_levels=top_levels, editable_sources=editable_sources, distributions=distributions)
