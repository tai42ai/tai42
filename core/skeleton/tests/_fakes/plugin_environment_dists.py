"""Fake installed distributions laid out the way pip writes them into a plugin environment build.

:class:`LayingPip` is a pip runner that writes them into the ``--prefix`` it is given (a layer
build) or into the virtual environment of the ``--python`` it is given (a server build).
"""

from __future__ import annotations

import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path

from tai42_skeleton.marketplace.environments.layout import layer_dir, layer_site_dirs, running_python, server_site_dirs
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.errors import PipFailedError


def write_dist_info(
    site: Path,
    package: str,
    version: str | None,
    *,
    requires: list[str] | None = None,
    console_scripts: Sequence[str] = (),
    recorded: Sequence[str] = (),
) -> Path:
    """Write ``package``'s ``.dist-info`` (``METADATA`` and ``RECORD``) into ``site`` and return it.

    ``METADATA`` names the package, its ``version`` (no ``Version`` line when ``None``) and a
    ``Requires-Dist`` line per entry of ``requires``; ``console_scripts`` names the scripts its
    ``entry_points.txt`` declares; ``recorded`` names further site-relative files ``RECORD`` lists.
    """
    info = site / f"{package.replace('-', '_')}-{version or '0'}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {package}"]
    if version is not None:
        lines.append(f"Version: {version}")
    lines += [f"Requires-Dist: {requirement}" for requirement in requires or ()]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    record = [f"{package}/__init__.py,,", *(f"{path},," for path in recorded)]
    record += [f"{info.name}/METADATA,,", f"{info.name}/RECORD,,"]
    if console_scripts:
        lines = ["[console_scripts]", *(f"{script} = {package.replace('-', '_')}:main" for script in console_scripts)]
        (info / "entry_points.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        record.append(f"{info.name}/entry_points.txt,,")
    (info / "RECORD").write_text("\n".join(record) + "\n", encoding="utf-8")
    return info


def install_fake_dist(build: Path, package: str, version: str, *, requires: list[str] | None = None) -> Path:
    """Write a minimal installed distribution into ``build``'s site directory and return that site directory.

    A package directory whose ``__init__.py`` reads ``VALUE = 1``, and its ``.dist-info``
    (:func:`write_dist_info`).
    """
    site = layer_site_dirs(build)[0]
    package_dir = site / package
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    write_dist_info(site, package, version, requires=requires)
    return site


def write_venv_config(build: Path, python: str | None = None) -> Path:
    """Lay out ``build`` as a virtual environment of Python ``python`` (default: the running one) is; return its site.

    Writes the ``pyvenv.cfg`` recording that version and makes ``lib/python<version>/site-packages``,
    the directories ``venv`` creates for that interpreter.
    """
    version = python or running_python()
    build.mkdir(parents=True, exist_ok=True)
    (build / "pyvenv.cfg").write_text(
        f"home = /usr/bin\ninclude-system-site-packages = false\nversion = {version}.0\n", encoding="utf-8"
    )
    site = build / "lib" / f"python{version}{sysconfig.get_config_var('abi_thread') or ''}" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    return site


def install_fake_server_dist(
    build: Path,
    package: str,
    version: str,
    *,
    console_scripts: Sequence[str] = (),
    files: Mapping[str, str] | None = None,
    python: str | None = None,
) -> Path:
    """Write ``package`` into the server virtual environment at ``build`` with its executable console scripts.

    A ``build`` with no ``pyvenv.cfg`` is first laid out as a venv of Python ``python``
    (:func:`write_venv_config`). The distribution's ``.dist-info`` (:func:`write_dist_info`)
    goes into the venv's site directory and each of ``console_scripts`` into ``bin``; ``files``
    maps site-relative paths the distribution also installs, and its ``RECORD`` lists, to their
    text. Returns the site directory.
    """
    site = server_site_dirs(build)[0] if (build / "pyvenv.cfg").is_file() else write_venv_config(build, python)
    for relative, body in (files or {}).items():
        (site / relative).parent.mkdir(parents=True, exist_ok=True)
        (site / relative).write_text(body, encoding="utf-8")
    write_dist_info(site, package, version, console_scripts=console_scripts, recorded=list(files or ()))
    bin_dir = build / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for script in console_scripts:
        (bin_dir / script).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / script).chmod(0o755)
    return site


def lay_server_requirement(
    args: Sequence[str],
    console_scripts: Mapping[str, Sequence[str]],
    files: Mapping[str, Mapping[str, str]] | None = None,
) -> None:
    """Lay the distribution a server build's pip call pins into its venv, as pip installs it.

    A no-op for any call that is not a server build's (no ``--python``) or whose requirement
    is not a ``name==version`` pin. The distribution declares ``console_scripts[name]`` and
    installs ``files[name]`` (:func:`install_fake_server_dist`).
    """
    if "--python" not in args or "==" not in args[-1]:
        return
    venv = Path(args[args.index("--python") + 1]).parent.parent
    name, version = args[-1].split("==", 1)
    install_fake_server_dist(
        venv, name, version, console_scripts=console_scripts.get(name, ()), files=(files or {}).get(name)
    )


def current_layer_build(plugins_dir: Path, build_id: str = "20260101T000000Z-000000") -> Path:
    """Create the plugin layer build ``build_id`` under ``plugins_dir``, point ``current`` at it, return it.

    The build holds its site directories and no build record; the caller lays distributions into it.
    """
    environment = layer_dir(plugins_dir)
    build = environment.build(build_id)
    for site_dir in layer_site_dirs(build):
        site_dir.mkdir(parents=True, exist_ok=True)
    point_current(environment, build_id)
    return build


class LayingPip:
    """A pip runner that records each call and lays distributions into its target.

    A layer build's call lays ``dists`` (name, version, requires) into its ``--prefix``. A
    server build's call lays the distribution it pins (:func:`lay_server_requirement`) and
    ``server_dependencies`` into the virtual environment whose ``--python`` it passes.
    ``console_scripts`` maps a server distribution's name to the scripts it declares.
    ``fail`` makes every call raise :class:`PipFailedError` with that output; ``constraints``
    holds the content of each call's ``--constraint`` file.
    """

    def __init__(
        self,
        dists: list[tuple[str, str, list[str]]] | None = None,
        *,
        server_dependencies: list[tuple[str, str]] | None = None,
        console_scripts: Mapping[str, Sequence[str]] | None = None,
        fail: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.constraints: list[str] = []
        self._dists = dists or []
        self._server_dependencies = server_dependencies or []
        self._console_scripts = console_scripts or {}
        self._fail = fail

    async def __call__(self, args: list[str], /, *, env: Mapping[str, str] | None = None) -> str:
        self.calls.append(args)
        self.envs.append(env)
        if "--constraint" in args:
            self.constraints.append(Path(args[args.index("--constraint") + 1]).read_text(encoding="utf-8"))
        if self._fail is not None:
            raise PipFailedError(args, 1, self._fail)
        if "--prefix" in args:
            site = layer_site_dirs(Path(args[args.index("--prefix") + 1]))[0]
            for name, version, requires in self._dists:
                write_dist_info(site, name, version, requires=requires)
        elif "--python" in args:
            lay_server_requirement(args, self._console_scripts)
            venv = Path(args[args.index("--python") + 1]).parent.parent
            for name, version in self._server_dependencies:
                install_fake_server_dist(venv, name, version, console_scripts=self._console_scripts.get(name, ()))
        return "pip ok"
