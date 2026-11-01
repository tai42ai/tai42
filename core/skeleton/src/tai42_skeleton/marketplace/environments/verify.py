"""Whether the current plugin environments match the install store and this image.

:func:`verify_environments` reads only: for the layer and every server package the store
names, it compares the ``current`` build's record with the store's pins, the interpreter and
platform it was built for with the running ones, and, for the layer, which plugins it installed
and left out with the ones this image's ``tai42-contract`` supports (:mod:`.quarantine`) and its
distributions with the image's (:mod:`.consistency`). A plugin the layer's record lists as left
out is still one of the layer's plugins. Each environment carries at most one
:class:`~.findings.Finding`, whose text reads the same for ``tai doctor``, for ``tai plugins
rebuild`` and at boot; a ``current`` build without a record, or whose record does not read, is one
(:func:`~.current_record.current_record`), which the rebuild replaces. A server
directory the store does not name is an orphan: one holding a ``current`` build is out of
date, since its commands are exposed for a plugin the store does not hold; one without is
left over (:data:`LEFT_OVER`).
"""

from __future__ import annotations

import sysconfig
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tai42_skeleton.marketplace.compat import running_contract_version
from tai42_skeleton.marketplace.environments.consistency import overlaps, unmet_requirements
from tai42_skeleton.marketplace.environments.current_record import current_record
from tai42_skeleton.marketplace.environments.findings import LAYER_SUBJECT, REBUILD_DEPLOYMENT, REBUILD_IMAGE, Finding
from tai42_skeleton.marketplace.environments.image import distributions_by_name, image_distributions
from tai42_skeleton.marketplace.environments.layout import (
    BuildInput,
    BuildRecord,
    EnvironmentDir,
    layer_dir,
    layer_site_dirs,
    running_python,
    server_dir,
    venv_python_version,
)
from tai42_skeleton.marketplace.environments.quarantine import partition
from tai42_skeleton.marketplace.environments.targets import PluginPin, Targets, store_targets, unnamed_server_dirs
from tai42_skeleton.marketplace.store import InstallRecord

#: How a server directory the store does not name and that holds no ``current`` build reads.
LEFT_OVER = (
    "left over: the install store names no plugin in it; the next install, update or uninstall of a "
    "packaged plugin, or 'tai plugins rebuild', deletes it"
)


@dataclass(frozen=True)
class EnvironmentStatus:
    """One environment as verified: what the store pins in it, its current build's record, its finding."""

    label: str
    environment: EnvironmentDir
    pins: tuple[PluginPin, ...]
    record: BuildRecord | None
    finding: Finding | None
    server_package: str | None = None

    def summary(self) -> str:
        """``<n> plugins, built <when>`` for a passing environment."""
        built = "no build" if self.record is None else f"built {self.record.built_at}"
        return f"{len(self.pins)} plugins, {built}"


@dataclass(frozen=True)
class Verification:
    """Every environment of the plugins directory against the install store.

    ``environments`` holds the layer and each server package the store names; ``orphans``
    each server directory it does not name, with no pins. ``unset`` is the finding when the
    store records packaged plugins and ``TAI_PLUGINS_DIR`` is unset; then no environment is read.
    """

    plugins_dir: Path | None
    targets: Targets
    environments: tuple[EnvironmentStatus, ...] = ()
    orphans: tuple[EnvironmentStatus, ...] = ()
    unset: Finding | None = None

    @property
    def findings(self) -> list[Finding]:
        """Every finding, the unset directory's first, then the environments', then the orphans'."""
        found = [] if self.unset is None else [self.unset]
        statuses = (*self.environments, *self.orphans)
        return found + [status.finding for status in statuses if status.finding is not None]


def verify_environments(plugins_dir: Path | None, rows: Iterable[InstallRecord]) -> Verification:
    """Verify every environment ``plugins_dir`` holds against the install store's ``rows``."""
    targets = store_targets(rows)
    if plugins_dir is None:
        refs = [pin.input.ref for pin in (*targets.layer, *targets.servers.values())]
        if not refs:
            return Verification(plugins_dir=None, targets=targets)
        unset = Finding(
            head="installed plugins need TAI_PLUGINS_DIR",
            detail=(
                f"{len(refs)} packaged plugins are recorded ({', '.join(sorted(refs))}); "
                "set TAI_PLUGINS_DIR to the directory they were installed into"
            ),
        )
        return Verification(plugins_dir=None, targets=targets, unset=unset)
    statuses = [_verify_layer(plugins_dir, targets.layer)]
    statuses += [_verify_server(plugins_dir, name, pin) for name, pin in sorted(targets.servers.items())]
    return Verification(
        plugins_dir=plugins_dir,
        targets=targets,
        environments=tuple(statuses),
        orphans=tuple(_orphans(plugins_dir, targets)),
    )


def _orphans(plugins_dir: Path, targets: Targets) -> list[EnvironmentStatus]:
    statuses: list[EnvironmentStatus] = []
    for environment in unnamed_server_dirs(plugins_dir, targets):
        package = environment.root.name
        subject = _server_subject(package)
        current = current_record(environment, subject)
        finding = current.finding or _store_mismatch(plugins_dir, subject, (), current.build_id, current.record)
        statuses.append(
            EnvironmentStatus(
                label=f"server {package}",
                environment=environment,
                pins=(),
                record=current.record,
                finding=finding,
                server_package=package,
            )
        )
    return statuses


def _server_subject(package: str) -> str:
    return f"plugin environment of server package {package}"


def _store_mismatch(
    plugins_dir: Path, subject: str, pins: Sequence[PluginPin], build_id: str | None, record: BuildRecord | None
) -> Finding | None:
    """The out-of-date finding when the current build is missing or its readable ``record`` holds other inputs."""
    wanted = {pin.input for pin in pins}
    if build_id is None and not wanted:
        return None
    held: set[BuildInput] = set()
    if record is not None:
        held = {*record.inputs, *(excluded.input for excluded in record.excluded)}
    if build_id is not None and record is not None and held == wanted:
        return None
    parts = [f"missing {item.label()}" for item in sorted(wanted - held)]
    parts += [f"holds {item.label()} not recorded" for item in sorted(held - wanted)]
    if build_id is None:
        parts.insert(0, "no current build")
    return Finding(
        head=f"{subject} is out of date",
        detail=f"the plugins directory {plugins_dir} does not match the install store ({', '.join(parts)})",
        tail=REBUILD_DEPLOYMENT,
    )


def _quarantine_stale(subject: str, pins: Sequence[PluginPin], record: BuildRecord) -> Finding | None:
    """The out-of-date finding when the record installed a plugin this contract rejects, or left out one it supports."""
    split = partition(pins)
    rejected = {excluded.input for excluded in split.excluded}
    supported = {pin.input for pin in split.included}
    contract = running_contract_version()
    parts = [
        f"it holds {item.label()}, which this image's tai42-contract {contract} does not support"
        for item in record.inputs
        if item in rejected
    ]
    parts += [
        f"it leaves out {excluded.input.label()}, which this image's tai42-contract {contract} supports"
        for excluded in record.excluded
        if excluded.input in supported
    ]
    if not parts:
        return None
    return Finding(head=f"{subject} is out of date", detail="; ".join(parts), tail=REBUILD_IMAGE)


def _interpreter_mismatch(subject: str, python: str, platform: str) -> Finding | None:
    running = running_python()
    running_platform = sysconfig.get_platform()
    if python == running and platform == running_platform:
        return None
    return Finding(
        head=(
            f"{subject} was built for Python {python} on {platform} "
            f"but this image runs Python {running} on {running_platform}"
        ),
        tail=REBUILD_IMAGE,
    )


def _verify_layer(plugins_dir: Path, pins: tuple[PluginPin, ...]) -> EnvironmentStatus:
    environment = layer_dir(plugins_dir)
    current = current_record(environment, LAYER_SUBJECT)
    build_id, record = current.build_id, current.record
    finding = current.finding or _store_mismatch(plugins_dir, LAYER_SUBJECT, pins, build_id, record)
    if finding is None and build_id is not None and record is not None:
        finding = (
            _quarantine_stale(LAYER_SUBJECT, pins, record)
            or _interpreter_mismatch(LAYER_SUBJECT, record.python, record.platform)
            or _image_conflict(environment, build_id)
        )
    return EnvironmentStatus(label="layer", environment=environment, pins=pins, record=record, finding=finding)


def _image_conflict(environment: EnvironmentDir, build_id: str) -> Finding | None:
    layer = distributions_by_name([str(site_dir) for site_dir in layer_site_dirs(environment.build(build_id))])
    image = image_distributions(environment.root)
    shared = overlaps(layer, image)
    if shared:
        first = shared[0]
        return Finding(
            head="plugin environment conflicts with this image",
            detail=(
                f"{first.name} {first.layer_version} is in the plugin layer"
                f" and the image provides {first.image_version}"
            ),
            tail=REBUILD_IMAGE,
        )
    unmet = unmet_requirements(layer, image)
    if unmet:
        return Finding(
            head="plugin environment is inconsistent with this image",
            detail=unmet[0].describe(),
            tail=REBUILD_IMAGE,
        )
    return None


def _verify_server(plugins_dir: Path, package: str, pin: PluginPin) -> EnvironmentStatus:
    environment = server_dir(plugins_dir, package)
    subject = _server_subject(package)
    current = current_record(environment, subject)
    build_id, record = current.build_id, current.record
    finding = current.finding or _store_mismatch(plugins_dir, subject, (pin,), build_id, record)
    if finding is None and build_id is not None and record is not None:
        build = environment.build(build_id)
        python = build / "bin" / "python"
        venv_version = venv_python_version(build)
        if not python.exists():
            finding = Finding(head=f"{subject} has no interpreter at {python}", tail=REBUILD_IMAGE)
        elif venv_version is None:
            finding = Finding(head=f"{subject} records no Python version in {build / 'pyvenv.cfg'}", tail=REBUILD_IMAGE)
        else:
            finding = _interpreter_mismatch(subject, venv_version, record.platform) or _interpreter_mismatch(
                subject, record.python, record.platform
            )
    return EnvironmentStatus(
        label=f"server {package}",
        environment=environment,
        pins=(pin,),
        record=record,
        finding=finding,
        server_package=package,
    )
