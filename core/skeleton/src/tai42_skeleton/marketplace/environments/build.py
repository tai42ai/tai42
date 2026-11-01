"""Building one plugin environment into a fresh directory, from its pins.

The plugin layer is one ``pip install --prefix`` of every in-process plugin this image's
``tai42-contract`` supports, together, constrained to the image's installed versions, made
under ``builds/.tmp-<build id>`` and renamed into place once complete; a plugin whose declared
contract range the running contract is outside is left out and recorded as excluded
(:mod:`.quarantine`). A server package's virtual environment is created with ``venv`` and
filled with ``pip --python``, with the image's ``PIP_CONSTRAINT`` removed from pip's
environment (the venv has its own closure); it is made at its final path because a venv's
console scripts name its interpreter by absolute path; every command its ``mcp-server`` items
name must be a console script its package declares, and no console script it declares may be
one another server package the install store holds already exposes. Either build's record, ``tai42-build.json``, is
written last, after pip and the post-build checks pass; any failure deletes the build
directory and raises.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import venv
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from tai42_skeleton.marketplace.environments.consistency import overlaps, unmet_requirements
from tai42_skeleton.marketplace.environments.console_scripts import package_console_scripts, server_script_owners
from tai42_skeleton.marketplace.environments.image import (
    distributions_by_name,
    environment_constraints,
    image_distributions,
)
from tai42_skeleton.marketplace.environments.layout import (
    EnvironmentDir,
    Excluded,
    artifacts_dir,
    layer_dir,
    layer_site_dirs,
    new_build_id,
    server_dir,
    write_build_record,
)
from tai42_skeleton.marketplace.environments.quarantine import partition
from tai42_skeleton.marketplace.environments.swap import remove_environment
from tai42_skeleton.marketplace.environments.targets import PluginPin
from tai42_skeleton.marketplace.errors import (
    PipFailedError,
    PluginDependencyConflictError,
    PluginsDirError,
    ServerCommandCollisionError,
    UndeclaredServerCommandError,
)
from tai42_skeleton.marketplace.pip import (
    PipRunner,
    fetch_verified_artifact,
    layer_install_args,
    pin_requirement,
    server_install_args,
)

_RESOLUTION_IMPOSSIBLE = "ResolutionImpossible"


@dataclass(frozen=True)
class Build:
    """A complete, committed build of one environment, the plugins it left out, and whether it created the directory.

    ``created_environment`` is true when the environment's directory did not exist before
    this build, so discarding the build removes the whole directory.
    """

    environment: EnvironmentDir
    build_id: str
    pip_output: str | None
    excluded: tuple[Excluded, ...] = ()
    created_environment: bool = False

    @property
    def path(self) -> Path:
        """The build's directory."""
        return self.environment.build(self.build_id)


def ensure_writable(plugins_dir: Path, directory: Path) -> None:
    """Create ``directory`` and confirm it is writable; otherwise raise :class:`PluginsDirError`.

    The error names ``plugins_dir``, the directory the operator mounts.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".tai42-write-probe-"):
            pass
    except OSError as exc:
        raise PluginsDirError(
            f"plugins directory {plugins_dir} is not writable: {exc} — "
            "mount it writable to install, update or rebuild plugins"
        ) from exc


async def requirement_for(plugins_dir: Path, pin: PluginPin) -> str:
    """The requirement pip installs for ``pin``.

    A github pin installs its archive from ``artifacts/<sha256>.tar.gz``, fetched and verified
    by :func:`fetch_verified_artifact` when it is not there yet.
    """
    item = pin.input
    if item.source != "github":
        return pin_requirement(item.package, item.version, item.source)
    if item.sha256 is None or pin.artifact_ref is None:
        raise PluginsDirError(f"{item.label()} is a github install with no recorded artifact and sha256")
    artifacts = artifacts_dir(plugins_dir)
    archive = artifacts / f"{item.sha256.lower()}.tar.gz"
    if not archive.is_file():
        ensure_writable(plugins_dir, artifacts)
        with tempfile.TemporaryDirectory(dir=artifacts, prefix=".fetch-") as tmp:
            fetched = await fetch_verified_artifact(
                item.package, item.version, pin.artifact_ref, item.sha256, Path(tmp)
            )
            os.replace(fetched, archive)
    return pin_requirement(item.package, item.version, item.source, archive)


def _resolution_failure(output: str) -> str:
    """Pip's report from its first ``ERROR:`` line on."""
    start = output.find("ERROR:")
    return (output if start < 0 else output[start:]).strip()


async def build_layer(plugins_dir: Path, pins: Sequence[PluginPin], *, pip_runner: PipRunner, subject: str) -> Build:
    """Build the plugin layer from ``pins`` in one pip call and commit it under ``layer/builds/``.

    A pin whose declared contract range the running ``tai42-contract`` is outside is not
    installed: the record lists it as excluded with the compat verdict's reason, and so does
    the returned :class:`Build`. No pin left to install builds an empty layer: the directory
    layout and its record, with no pip call. ``subject`` opens every refusal (``cannot install
    <ref> <version>``, ``cannot rebuild the plugin layer``): pip finding no resolution raises
    :class:`PluginDependencyConflictError` with pip's report; a build holding a distribution the
    image also provides, or one whose requirement the layer and the image do not meet, raises
    the same, naming it.
    """
    environment = layer_dir(plugins_dir)
    created = not environment.root.exists()
    ensure_writable(plugins_dir, environment.builds)
    build_id = new_build_id()
    tmp = environment.tmp_build(build_id)
    tmp.mkdir()
    pip_output: str | None = None
    split = partition(pins)
    try:
        if split.included:
            pip_output = await _install_layer(plugins_dir, tmp, split.included, pip_runner=pip_runner, subject=subject)
        else:
            for site_dir in layer_site_dirs(tmp):
                site_dir.mkdir(parents=True, exist_ok=True)
        write_build_record(tmp, [pin.input for pin in split.included], list(split.excluded))
        tmp.rename(environment.build(build_id))
    except BaseException:
        if tmp.exists():
            shutil.rmtree(tmp)
        raise
    return Build(
        environment=environment,
        build_id=build_id,
        pip_output=pip_output,
        excluded=split.excluded,
        created_environment=created,
    )


async def _install_layer(
    plugins_dir: Path, build: Path, pins: Sequence[PluginPin], *, pip_runner: PipRunner, subject: str
) -> str:
    """Run the layer's one pip call into ``build``, check the result against the image, return pip's output."""
    layer_root = layer_dir(plugins_dir).root
    requirements = [await requirement_for(plugins_dir, pin) for pin in pins]
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "image-constraints.txt"
        constraints.write_text("".join(f"{line}\n" for line in environment_constraints(layer_root)), encoding="utf-8")
        try:
            output = await pip_runner(layer_install_args(build, constraints, requirements))
        except PipFailedError as exc:
            if _RESOLUTION_IMPOSSIBLE not in exc.output:
                raise
            raise PluginDependencyConflictError(
                f"{subject}: its dependencies conflict with the installed plugins or this image — "
                f"{_resolution_failure(exc.output)}"
            ) from exc
    layer = distributions_by_name([str(site_dir) for site_dir in layer_site_dirs(build)])
    image = image_distributions(layer_root)
    shared = overlaps(layer, image)
    if shared:
        overlap = shared[0]
        raise PluginDependencyConflictError(
            f"{subject}: it would add {overlap.name} {overlap.layer_version} beside the image's "
            f"{overlap.name} {overlap.image_version}; this image provides {overlap.name} {overlap.image_version} "
            "and a plugin cannot replace it"
        )
    unmet = unmet_requirements(layer, image)
    if unmet:
        raise PluginDependencyConflictError(f"{subject}: {unmet[0].describe()}")
    return output


def server_python(build: Path) -> Path:
    """The interpreter of the server virtual environment at ``build``."""
    return build / "bin" / "python"


def _server_pip_env() -> dict[str, str]:
    """This process's environment without ``PIP_CONSTRAINT``, which pins the image, not a server venv."""
    return {key: value for key, value in os.environ.items() if key != "PIP_CONSTRAINT"}


async def build_server(
    plugins_dir: Path, pin: PluginPin, *, servers: Collection[str], pip_runner: PipRunner, subject: str
) -> Build:
    """Build ``pin``'s own virtual environment and commit it under ``servers/<package>/builds/``.

    ``servers`` names the server packages whose ``current`` build the new one is judged
    against, by any spelling: an operation names those the install store holds once the build
    is in use, a rebuild those whose ``current`` build stays. A pip failure raises as pip
    reported it; a command one of the pin's ``mcp-server`` items names that is not a console
    script of its package raises :class:`UndeclaredServerCommandError`; a console script its package declares that the
    ``current`` build of another package in ``servers`` exposes raises
    :class:`ServerCommandCollisionError`, opened by ``subject`` (``cannot install <ref>
    <version>``). Each failure deletes the build directory, and the package's server directory
    when this build created it; the other package's build stays current.
    """
    environment = server_dir(plugins_dir, pin.input.package)
    created = not environment.root.exists()
    ensure_writable(plugins_dir, environment.builds)
    build_id = new_build_id()
    build = environment.build(build_id)
    try:
        await asyncio.to_thread(venv.EnvBuilder(symlinks=True, with_pip=False).create, build)
        requirement = await requirement_for(plugins_dir, pin)
        output = await pip_runner(server_install_args(server_python(build), requirement), env=_server_pip_env())
        _check_server_commands(build, pin)
        _check_script_collisions(plugins_dir, build, pin, servers, subject)
        write_build_record(build, [pin.input], [])
    except BaseException:
        if created:
            remove_environment(environment)
        elif build.exists():
            shutil.rmtree(build)
        raise
    return Build(environment=environment, build_id=build_id, pip_output=output, created_environment=created)


def _check_server_commands(build: Path, pin: PluginPin) -> None:
    """Refuse the build when a command ``pin``'s ``mcp-server`` items name is not a console script of its package."""
    if not pin.server_commands:
        return
    package = pin.input.package
    scripts = package_console_scripts(build, package)
    for entry in pin.server_commands:
        if entry.command not in scripts:
            names = ", ".join(scripts) or "none"
            raise UndeclaredServerCommandError(
                f"{pin.input.ref}: mcp-server item {entry.item} names command {entry.command!r}, which is not a "
                f"console script of package {package} (its scripts: {names}); an mcp-server command is a "
                "console script the package declares"
            )


def _check_script_collisions(
    plugins_dir: Path, build: Path, pin: PluginPin, servers: Collection[str], subject: str
) -> None:
    """Refuse the build when a console script ``pin``'s package declares is exposed by another of ``servers``."""
    package = pin.input.package
    others = [name for name in servers if canonicalize_name(name) != canonicalize_name(package)]
    exposed = server_script_owners(plugins_dir, others)
    for name in package_console_scripts(build, package):
        owner = exposed.get(name)
        if owner is not None:
            raise ServerCommandCollisionError(
                f"{subject}: its package {package} declares the console script {name!r}, which the installed "
                f"server package {owner.package} already exposes; a bare mcp command names one package's script "
                f"— uninstall the plugin that provides {owner.package} first, or choose a release that does not "
                f"declare {name!r}"
            )
