"""Rebuilding the plugin environments that fail verification, from the install store.

:func:`rebuild_environments` verifies every environment (:func:`.verify.verify_environments`),
first removes each server directory the store does not name, so none of its commands stays
exposed beside a rebuilt one, then builds a fresh environment for each with a finding
(re-fetching a missing github artifact), makes it current, and deletes every build the
``current`` symlinks do not name. A server build's console scripts are judged against the
servers that verified and those already rebuilt, never against a build this rebuild
replaces. A rebuilt layer's detail names each plugin it left out and why (``left out <ref>
<version>: <reason>``). The caller holds the fleet lock, so no marketplace operation changes
the directory meanwhile. A build failure raises with pip's text.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from tai42_skeleton.marketplace.environments.build import Build, build_layer, build_server
from tai42_skeleton.marketplace.environments.swap import point_current, prune, remove_environment
from tai42_skeleton.marketplace.environments.verify import EnvironmentStatus, verify_environments
from tai42_skeleton.marketplace.pip import PipRunner, ensure_pip_available
from tai42_skeleton.marketplace.store import InstallRecord

UP_TO_DATE = "up to date"
REBUILT = "rebuilt"
REMOVED = "removed"


@dataclass(frozen=True)
class RebuildOutcome:
    """What the rebuild did to one environment: its ``label``, ``action`` and ``detail``."""

    label: str
    action: str
    detail: str


@dataclass(frozen=True)
class Rebuild:
    """Every environment's outcome, and whether the layer was rebuilt while it serves in-process plugins."""

    outcomes: tuple[RebuildOutcome, ...]
    layer_rebuilt_with_plugins: bool


async def rebuild_environments(plugins_dir: Path, rows: Iterable[InstallRecord], *, pip_runner: PipRunner) -> Rebuild:
    """Rebuild each environment of ``plugins_dir`` that does not match ``rows`` and this image; prune the rest."""
    verification = verify_environments(plugins_dir, rows)
    stale = [status for status in verification.environments if status.finding is not None]
    if stale:
        ensure_pip_available()
    outcomes: list[RebuildOutcome] = []
    for orphan in verification.orphans:
        remove_environment(orphan.environment)
        outcomes.append(RebuildOutcome(orphan.label, REMOVED, "the install store names no plugin in it"))
    layer_rebuilt_with_plugins = False
    # The servers whose current build stays: the ones that verified, then each one rebuilt here.
    settled = {
        status.server_package
        for status in verification.environments
        if status.server_package is not None and status.finding is None
    }
    for status in verification.environments:
        if status.finding is None:
            prune(status.environment)
            outcomes.append(RebuildOutcome(status.label, UP_TO_DATE, status.summary()))
            continue
        build = await _rebuild(plugins_dir, status, servers=frozenset(settled), pip_runner=pip_runner)
        if status.server_package is not None:
            settled.add(status.server_package)
        prune(status.environment)
        detail = "".join([status.finding.report_text(), *(f"; {item.left_out()}" for item in build.excluded)])
        outcomes.append(RebuildOutcome(status.label, REBUILT, detail))
        if status.server_package is None and status.pins:
            layer_rebuilt_with_plugins = True
    return Rebuild(outcomes=tuple(outcomes), layer_rebuilt_with_plugins=layer_rebuilt_with_plugins)


async def _rebuild(
    plugins_dir: Path, status: EnvironmentStatus, *, servers: Collection[str], pip_runner: PipRunner
) -> Build:
    if status.server_package is None:
        build = await build_layer(
            plugins_dir, status.pins, pip_runner=pip_runner, subject="cannot rebuild the plugin layer"
        )
    else:
        pin = status.pins[0]
        build = await build_server(
            plugins_dir,
            pin,
            servers=servers,
            pip_runner=pip_runner,
            subject=f"cannot rebuild {pin.input.label()}",
        )
    point_current(status.environment, build.build_id)
    return build
