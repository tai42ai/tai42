"""One marketplace operation's environment changes: build, swap, swap back or finish.

An install, update or uninstall of a packaged plugin changes the environments its old and
new pins live in, and the server directories the install store does not name: the plugin
layer when either pin is an in-process plugin, rebuilt whole from the store's pins after the
operation; the new pin's own server environment, built fresh; and every server directory no
pin lives in after the operation (the old pin's, when the new pin no longer lives there, and
any an earlier operation left), removed. :func:`prepare_change` builds them beside the ones in
use; :meth:`OperationChange.swap` makes the builds current and withdraws the ``current`` of
each server directory being removed, so its commands are no longer exposed; a later failure
calls :meth:`OperationChange.swap_back`, which restores the previous builds with no pip call;
once the operation's install store write is done, :meth:`OperationChange.finish` deletes every
other build and every removed server's whole directory.
"""

from __future__ import annotations

import importlib.metadata
import logging
from dataclasses import dataclass, field
from pathlib import Path

from tai42_skeleton.marketplace.environments.build import Build, build_layer, build_server
from tai42_skeleton.marketplace.environments.image import distributions_by_name
from tai42_skeleton.marketplace.environments.layout import EnvironmentDir, layer_site_dirs
from tai42_skeleton.marketplace.environments.swap import (
    current_build,
    delete_build,
    point_current,
    prune,
    remove_environment,
    swap_back,
)
from tai42_skeleton.marketplace.environments.targets import PlacedPin, Targets, unnamed_server_dirs
from tai42_skeleton.marketplace.pip import PipRunner

logger = logging.getLogger(__name__)

_EXTENSION_SUFFIXES = (".so", ".pyd")


@dataclass
class EnvironmentChange:
    """The environment an operation changes, the build it made (``None`` to remove a server), and the swap state."""

    environment: EnvironmentDir
    build: Build | None
    is_layer: bool
    previous: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def pip_output(self) -> str | None:
        """Pip's output for the new build; ``None`` when no pip ran."""
        return None if self.build is None else self.build.pip_output

    def swap(self) -> None:
        """Make the new build current, or withdraw a removed server's ``current``; note compiled modules kept."""
        self.previous = current_build(self.environment)
        if self.build is None:
            if self.previous is not None:
                self.environment.current.unlink()
            return
        if self.is_layer and self.previous is not None:
            self.notes += compiled_module_notes(
                self.environment.build(self.previous), self.environment.build(self.build.build_id)
            )
            for note in self.notes:
                logger.warning("marketplace: %s", note)
        point_current(self.environment, self.build.build_id)

    def swap_back(self) -> None:
        """Restore the build that was current before :meth:`swap` and delete the new one.

        A new build that created its environment's directory takes the whole directory with it.
        """
        if self.build is None:
            if self.previous is not None:
                point_current(self.environment, self.previous)
        elif self.build.created_environment:
            remove_environment(self.environment)
        else:
            swap_back(self.environment, self.previous, self.build.build_id)

    def discard(self) -> None:
        """Delete the new build that was never made current, with its directory when the build created it."""
        if self.build is None:
            return
        if self.build.created_environment:
            remove_environment(self.environment)
        else:
            delete_build(self.environment, self.build.build_id)

    def finish(self) -> list[str]:
        """Delete every build but the current one, or the removed server's directory; return the operation's notes.

        Runs after the operation committed, so a deletion that fails is logged and returned as a
        note naming what is left. ``tai plugins rebuild`` deletes it, and so does the next
        operation that changes this environment; a removed server's directory, whose ``current``
        is already withdrawn, is deleted by the next operation on any packaged plugin.
        """
        try:
            if self.build is None:
                remove_environment(self.environment)
            else:
                prune(self.environment)
        except OSError as exc:
            note = f"could not delete the unused plugin builds under {self.environment.root}: {exc}"
            logger.exception("marketplace: %s", note)
            self.notes.append(note)
        return self.notes


@dataclass
class OperationChange:
    """Every environment one operation changes, swapped, swapped back and finished together."""

    changes: list[EnvironmentChange]

    @property
    def pip_output(self) -> str | None:
        """Pip's output for the operation's builds, joined; ``None`` when no pip ran."""
        outputs = [change.pip_output for change in self.changes if change.pip_output]
        return "\n".join(outputs) if outputs else None

    def swap(self) -> None:
        """Make every new build current."""
        for change in self.changes:
            change.swap()

    def swap_back(self) -> None:
        """Restore every environment's previous build, in reverse order."""
        for change in reversed(self.changes):
            change.swap_back()

    @property
    def restores_builds(self) -> bool:
        """Whether a swap back changes an environment: drops a new build or puts a withdrawn ``current`` back."""
        return any(change.build is not None or change.previous is not None for change in self.changes)

    def finish(self) -> list[str]:
        """Finish every change (:meth:`EnvironmentChange.finish`) and return their notes."""
        notes: list[str] = []
        for change in self.changes:
            notes += change.finish()
        return notes


async def prepare_change(
    plugins_dir: Path,
    targets: Targets,
    old: PlacedPin | None,
    new: PlacedPin | None,
    *,
    pip_runner: PipRunner,
    subject: str,
) -> OperationChange:
    """Build the environments an operation moving a plugin from ``old`` to ``new`` changes.

    ``targets`` is the store's pins after the operation. ``old`` is ``None`` for an install,
    ``new`` for an uninstall. The layer is rebuilt from ``targets.layer`` when either pin is an
    in-process plugin; every server directory ``targets`` does not name (``old``'s, when ``new``
    does not live in it, among them) is withdrawn by :meth:`OperationChange.swap` and removed by
    :meth:`OperationChange.finish`; ``new``'s server environment is built when it is an
    ``mcp-server`` package, its console scripts checked against the other server packages
    ``targets`` names. ``subject`` opens a build's refusal. A layer build that leaves a plugin
    out notes it (``left out <ref> <version>: <reason>``) among the operation's notes. A build
    that fails raises after deleting the builds this call already made.
    """
    changes: list[EnvironmentChange] = []
    try:
        if (old is not None and not old.runs_as_server) or (new is not None and not new.runs_as_server):
            build = await build_layer(plugins_dir, targets.layer, pip_runner=pip_runner, subject=subject)
            notes = [excluded.left_out() for excluded in build.excluded]
            changes.append(EnvironmentChange(environment=build.environment, build=build, is_layer=True, notes=notes))
        changes += [
            EnvironmentChange(environment=environment, build=None, is_layer=False)
            for environment in unnamed_server_dirs(plugins_dir, targets)
        ]
        if new is not None and new.runs_as_server:
            build = await build_server(
                plugins_dir, new.pin, servers=targets.servers.keys(), pip_runner=pip_runner, subject=subject
            )
            changes.append(EnvironmentChange(environment=build.environment, build=build, is_layer=False))
    except BaseException:
        for change in changes:
            change.discard()
        raise
    return OperationChange(changes)


def _has_compiled_modules(dist: importlib.metadata.Distribution) -> bool:
    return any(str(file).endswith(_EXTENSION_SUFFIXES) for file in dist.files or ())


def compiled_module_notes(old_build: Path, new_build: Path) -> list[str]:
    """A note for each distribution whose version changed between the builds and whose old build has compiled code."""
    old = distributions_by_name([str(site_dir) for site_dir in layer_site_dirs(old_build)])
    new = distributions_by_name([str(site_dir) for site_dir in layer_site_dirs(new_build)])
    notes: list[str] = []
    for name in sorted(old.keys() & new.keys()):
        before, after = old[name], new[name]
        old_version, new_version = before.metadata.get("Version"), after.metadata.get("Version")
        if old_version != new_version and _has_compiled_modules(before):
            notes.append(
                f"{after.metadata['Name']} {old_version} -> {new_version} has compiled modules that a running "
                "process cannot replace — restart the server to load it"
            )
    return notes
