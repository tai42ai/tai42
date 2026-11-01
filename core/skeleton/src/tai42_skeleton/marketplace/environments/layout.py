"""Where each plugin environment lives under the plugins directory, and what a build records.

::

    <plugins dir>/
      layer/builds/<build id>/            the plugin layer: a ``pip install --prefix`` target
      layer/current -> builds/<build id>
      servers/<package>/builds/<build id>/  one virtual environment per ``mcp-server`` package
      servers/<package>/current -> builds/<build id>
      artifacts/<sha256>.tar.gz           github-source archives, by content hash

A build is made under ``builds/.tmp-<build id>`` and carries its record,
``tai42-build.json``, written last: the plugins it was built from, the plugins it left out
and why, the Python version and the platform tag. A build directory without a record is
incomplete.
"""

from __future__ import annotations

import json
import secrets
import sys
import sysconfig
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from tai42_skeleton.marketplace.errors import PluginsDirError, UnreadableBuildRecordError

BUILD_RECORD = "tai42-build.json"
TMP_PREFIX = ".tmp-"


@dataclass(frozen=True)
class EnvironmentDir:
    """One environment's directory: its builds and the ``current`` symlink naming the one in use."""

    root: Path

    @property
    def builds(self) -> Path:
        """The directory every build of this environment lives in."""
        return self.root / "builds"

    @property
    def current(self) -> Path:
        """The symlink naming the build in use."""
        return self.root / "current"

    def build(self, build_id: str) -> Path:
        """The committed build ``build_id``."""
        return self.builds / build_id

    def tmp_build(self, build_id: str) -> Path:
        """The directory build ``build_id`` is made in before it is committed."""
        return self.builds / f"{TMP_PREFIX}{build_id}"


def layer_dir(plugins_dir: Path) -> EnvironmentDir:
    """The plugin layer's environment directory."""
    return EnvironmentDir(plugins_dir / "layer")


def servers_root(plugins_dir: Path) -> Path:
    """The directory holding one environment directory per ``mcp-server`` package."""
    return plugins_dir / "servers"


def server_dir(plugins_dir: Path, package: str) -> EnvironmentDir:
    """The environment directory of ``mcp-server`` package ``package``, by its normalized name."""
    return EnvironmentDir(servers_root(plugins_dir) / canonicalize_name(package))


def server_dirs(plugins_dir: Path) -> list[EnvironmentDir]:
    """Every server environment directory under ``plugins_dir``, by name; none when ``servers/`` does not exist."""
    root = servers_root(plugins_dir)
    if not root.is_dir():
        return []
    return [EnvironmentDir(entry) for entry in sorted(root.iterdir())]


def current_server_dirs(plugins_dir: Path) -> list[EnvironmentDir]:
    """Every server environment directory with a ``current`` build: the server packages in use, by name.

    A server directory the install store stops naming has its ``current`` withdrawn before it is
    deleted, so its package is no longer in use; one still holding a ``current`` is a
    verification finding.
    """
    return [environment for environment in server_dirs(plugins_dir) if environment.current.exists()]


def artifacts_dir(plugins_dir: Path) -> Path:
    """The directory github-source archives are kept in, by content hash."""
    return plugins_dir / "artifacts"


def layer_site_dirs(prefix: Path) -> list[Path]:
    """The site-package directories ``pip install --prefix <prefix>`` populates, ``purelib`` then ``platlib``."""
    return _site_dirs(sysconfig.get_paths(vars={"base": str(prefix), "platbase": str(prefix)}))


def current_layer_site_dirs(plugins_dir: Path) -> list[Path]:
    """The site-package directories of the layer's ``current`` build, through the symlink; none without one."""
    current = layer_dir(plugins_dir).current
    return layer_site_dirs(current) if current.exists() else []


def venv_python_version(prefix: Path) -> str | None:
    """The ``major.minor`` the virtual environment at ``prefix`` records in its ``pyvenv.cfg``, or ``None`` if none."""
    config = prefix / "pyvenv.cfg"
    if not config.is_file():
        return None
    for line in config.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("version", "version_info"):
            return ".".join(value.strip().split(".")[:2])
    return None


def server_site_dirs(prefix: Path) -> list[Path]:
    """The site-package directories of the virtual environment at ``prefix``, ``purelib`` then ``platlib``.

    They are named by the Python version the environment's own ``pyvenv.cfg`` records
    (:func:`venv_python_version`), so an environment an image with another Python built is read
    where its interpreter installed its packages. An environment recording no version raises
    :class:`PluginsDirError` naming its ``pyvenv.cfg``.
    """
    python = venv_python_version(prefix)
    if python is None:
        raise PluginsDirError(
            f"{prefix / 'pyvenv.cfg'} records no Python version, so the site directories of the server build are "
            "unknown; run 'tai plugins rebuild' against this image"
        )
    paths = sysconfig.get_paths(
        scheme="venv", vars={"base": str(prefix), "platbase": str(prefix), "py_version_short": python}
    )
    return _site_dirs(paths)


def _site_dirs(paths: dict[str, str]) -> list[Path]:
    dirs: list[Path] = []
    for key in ("purelib", "platlib"):
        directory = Path(paths[key])
        if directory not in dirs:
            dirs.append(directory)
    return dirs


def new_build_id() -> str:
    """A fresh build id: the UTC time to the second, then six hex characters."""
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def running_python() -> str:
    """The running interpreter's ``major.minor`` version."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


@dataclass(frozen=True, order=True)
class BuildInput:
    """One plugin a build was made from, as its install-store row names it."""

    ref: str
    package: str
    version: str
    source: str
    sha256: str | None

    def label(self) -> str:
        """``<ref> <version>``, the plugin as an operator reads it."""
        return f"{self.ref} {self.version}"


@dataclass(frozen=True, order=True)
class Excluded:
    """A plugin a layer build left out: its input, the import paths of its modules, and why."""

    input: BuildInput
    modules: tuple[str, ...]
    reason: str

    def left_out(self) -> str:
        """``left out <ref> <version>: <reason>``, the exclusion as an operator reads it."""
        return f"left out {self.input.label()}: {self.reason}"


@dataclass(frozen=True)
class BuildRecord:
    """What ``tai42-build.json`` records: the inputs, the exclusions, the interpreter and platform, and when."""

    inputs: tuple[BuildInput, ...]
    excluded: tuple[Excluded, ...]
    python: str
    platform: str
    built_at: str


def write_build_record(build: Path, inputs: list[BuildInput], excluded: list[Excluded]) -> BuildRecord:
    """Write ``build``'s record for ``inputs`` and ``excluded`` (each sorted) and the running interpreter; return it."""
    record = BuildRecord(
        inputs=tuple(sorted(inputs)),
        excluded=tuple(sorted(excluded)),
        python=running_python(),
        platform=sysconfig.get_platform(),
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    document = {
        "inputs": [asdict(item) for item in record.inputs],
        "excluded": [
            {**asdict(item.input), "reason": item.reason, "modules": list(item.modules)} for item in record.excluded
        ],
        "python": record.python,
        "platform": record.platform,
        "built_at": record.built_at,
    }
    (build / BUILD_RECORD).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return record


def read_build_record(build: Path) -> BuildRecord | None:
    """``build``'s record, or ``None`` when it has none (an incomplete build).

    A record that is not the document :func:`write_build_record` writes raises
    :class:`UnreadableBuildRecordError` naming the file and why.
    """
    path = build / BUILD_RECORD
    if not path.is_file():
        return None
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
        inputs = tuple(_build_input(item) for item in document["inputs"])
        excluded = tuple(
            Excluded(input=_build_input(item), modules=tuple(item["modules"]), reason=item["reason"])
            for item in document["excluded"]
        )
        return BuildRecord(
            inputs=inputs,
            excluded=excluded,
            python=document["python"],
            platform=document["platform"],
            built_at=document["built_at"],
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise UnreadableBuildRecordError(path, str(exc)) from exc


def _build_input(item: Any) -> BuildInput:
    return BuildInput(
        ref=item["ref"],
        package=item["package"],
        version=item["version"],
        source=item["source"],
        sha256=item["sha256"],
    )
