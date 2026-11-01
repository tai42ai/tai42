"""Building the plugin layer and a server package's environment into a fresh build directory.

pip is :class:`LayingPip`, which lays the distributions a case names into the ``--prefix``
directory it is given, so the post-build checks read real files; a server build creates a
real virtual environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from tests._fakes.plugin_environment_dists import LayingPip

from tai42_skeleton.marketplace.compat import declared_compat, running_contract_version
from tai42_skeleton.marketplace.environments import build as build_module
from tai42_skeleton.marketplace.environments.build import build_layer, build_server
from tai42_skeleton.marketplace.environments.image import environment_constraints
from tai42_skeleton.marketplace.environments.layout import (
    BuildInput,
    Excluded,
    layer_dir,
    layer_site_dirs,
    read_build_record,
    server_dir,
)
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.environments.targets import PluginPin, ServerCommand
from tai42_skeleton.marketplace.errors import (
    PipFailedError,
    PluginDependencyConflictError,
    PluginsDirError,
    ServerCommandCollisionError,
    UndeclaredServerCommandError,
)

_SUBJECT = "cannot install tai42/toolbox 1.0.0"
_SERVER_SUBJECT = "cannot install tai42/srv 1.0.0"
# The server packages the install store holds once a build is in use.
_SERVERS = ("tai42-srv", "tai42-other")


def _pin(
    name: str = "toolbox", version: str = "1.0.0", *, source: str = "pypi", sha256: str | None = None
) -> PluginPin:
    return PluginPin(
        input=BuildInput(ref=f"tai42/{name}", package=f"tai42-{name}", version=version, source=source, sha256=sha256),
        artifact_ref=None if source == "pypi" else f"https://codeload.github.com/tai42ai/{name}/tar.gz/refs/tags/v1",
    )


def _builds(environment_root: Path) -> list[str]:
    builds = environment_root / "builds"
    return sorted(entry.name for entry in builds.iterdir()) if builds.is_dir() else []


# -- the plugin layer ------------------------------------------------------------------


async def test_the_layer_is_one_pip_call_of_every_pin_constrained_to_the_image(tmp_path: Path) -> None:
    pip = LayingPip([("tai42-a", "1.0.0", []), ("tai42-b", "2.0.0", [])])

    build = await build_layer(tmp_path, [_pin("a"), _pin("b", "2.0.0")], pip_runner=pip, subject=_SUBJECT)

    assert len(pip.calls) == 1
    args = pip.calls[0]
    tmp_build = Path(args[args.index("--prefix") + 1])
    assert tmp_build == layer_dir(tmp_path).tmp_build(build.build_id)
    assert args[-2:] == ["tai42-a==1.0.0", "tai42-b==2.0.0"]
    # The constraints file pins every image distribution at its installed version.
    expected = "".join(f"{line}\n" for line in environment_constraints(layer_dir(tmp_path).root))
    assert pip.constraints == [expected]
    # The build was renamed into place with its record, written last.
    assert build.path == layer_dir(tmp_path).build(build.build_id)
    assert _builds(layer_dir(tmp_path).root) == [build.build_id]
    record = read_build_record(build.path)
    assert record is not None
    assert [item.label() for item in record.inputs] == ["tai42/a 1.0.0", "tai42/b 2.0.0"]
    assert build.pip_output == "pip ok"


async def test_an_empty_layer_is_built_with_no_pip_call(tmp_path: Path) -> None:
    pip = LayingPip()

    build = await build_layer(tmp_path, [], pip_runner=pip, subject=_SUBJECT)

    assert pip.calls == []
    assert all(site_dir.is_dir() for site_dir in layer_site_dirs(build.path))
    record = read_build_record(build.path)
    assert record is not None
    assert record.inputs == ()
    assert build.pip_output is None


# A contract range no running tai42-contract is inside, as a core upgrade leaves a store row.
_REJECTED_RANGE = ">=99,<100"


def _contract_pin(name: str, contract: str | None) -> PluginPin:
    pin = _pin(name)
    return PluginPin(
        input=pin.input, artifact_ref=None, contract=contract, modules=(f"tai42_{name}.tools", f"tai42_{name}.routes")
    )


async def test_a_pin_whose_contract_range_rejects_the_running_contract_is_left_out_of_the_layer(
    tmp_path: Path,
) -> None:
    pip = LayingPip([("tai42-a", "1.0.0", [])])
    kept = _contract_pin("a", f">={running_contract_version()}")
    rejected = _contract_pin("q", _REJECTED_RANGE)

    build = await build_layer(tmp_path, [kept, rejected], pip_runner=pip, subject=_SUBJECT)

    (call,) = pip.calls
    assert call[-1:] == ["tai42-a==1.0.0"]
    reason = declared_compat("tai42-q", _REJECTED_RANGE).reason
    assert reason is not None
    left_out = Excluded(input=rejected.input, modules=("tai42_q.tools", "tai42_q.routes"), reason=reason)
    assert build.excluded == (left_out,)
    record = read_build_record(build.path)
    assert record is not None
    assert record.inputs == (kept.input,)
    assert record.excluded == (left_out,)


async def test_a_layer_with_every_pin_excluded_builds_an_empty_layer_without_pip(tmp_path: Path) -> None:
    pip = LayingPip()

    build = await build_layer(tmp_path, [_contract_pin("q", _REJECTED_RANGE)], pip_runner=pip, subject=_SUBJECT)

    assert pip.calls == []
    assert all(site_dir.is_dir() for site_dir in layer_site_dirs(build.path))
    record = read_build_record(build.path)
    assert record is not None
    assert record.inputs == ()
    assert [item.input.label() for item in record.excluded] == ["tai42/q 1.0.0"]


async def test_pip_finding_no_resolution_is_a_dependency_conflict_naming_pip_s_report(tmp_path: Path) -> None:
    report = "Collecting x\nERROR: Cannot install tai42-a because these package versions have conflicting deps.\n"
    pip = LayingPip(fail=report + "ERROR: ResolutionImpossible: for help visit https://pip.pypa.io")

    with pytest.raises(PluginDependencyConflictError) as caught:
        await build_layer(tmp_path, [_pin("a")], pip_runner=pip, subject=_SUBJECT)

    assert str(caught.value).startswith(
        f"{_SUBJECT}: its dependencies conflict with the installed plugins or this image — ERROR: Cannot install"
    )
    assert "Collecting x" not in str(caught.value)
    assert _builds(layer_dir(tmp_path).root) == []


async def test_any_other_pip_failure_raises_as_pip_reported_it(tmp_path: Path) -> None:
    pip = LayingPip(fail="ERROR: No matching distribution found for tai42-a==1.0.0")

    with pytest.raises(PipFailedError) as caught:
        await build_layer(tmp_path, [_pin("a")], pip_runner=pip, subject=_SUBJECT)

    assert "No matching distribution" in caught.value.output

    assert _builds(layer_dir(tmp_path).root) == []


async def test_a_layer_holding_an_image_distribution_is_refused(tmp_path: Path) -> None:
    pip = LayingPip([("tai42-a", "1.0.0", []), ("pytest", "0.0.1", [])])

    with pytest.raises(PluginDependencyConflictError) as caught:
        await build_layer(tmp_path, [_pin("a")], pip_runner=pip, subject=_SUBJECT)

    assert re.fullmatch(
        rf"{re.escape(_SUBJECT)}: it would add pytest 0\.0\.1 beside the image's pytest (\S+); "
        r"this image provides pytest \1 and a plugin cannot replace it",
        str(caught.value),
    )
    assert _builds(layer_dir(tmp_path).root) == []


async def test_a_layer_requirement_neither_the_layer_nor_the_image_meets_is_refused(tmp_path: Path) -> None:
    pip = LayingPip([("tai42-a", "1.0.0", ["tai42-absent-dependency>=1"])])

    with pytest.raises(PluginDependencyConflictError) as caught:
        await build_layer(tmp_path, [_pin("a")], pip_runner=pip, subject=_SUBJECT)

    assert str(caught.value) == (
        f"{_SUBJECT}: tai42-a 1.0.0 requires tai42-absent-dependency>=1; "
        "neither the plugin layer nor the image provides tai42-absent-dependency"
    )
    assert _builds(layer_dir(tmp_path).root) == []


async def test_a_github_pin_installs_its_archive_kept_by_content_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches: list[str] = []

    async def fetch(package, version, artifact_ref, sha256, dest_dir):
        fetches.append(sha256)
        path = dest_dir / "download.tar.gz"
        path.write_bytes(b"archive")
        return path

    monkeypatch.setattr(build_module, "fetch_verified_artifact", fetch)
    pin = _pin("a", source="github", sha256="AB" * 32)
    pip = LayingPip([("tai42-a", "1.0.0", [])])

    await build_layer(tmp_path, [pin], pip_runner=pip, subject=_SUBJECT)
    await build_layer(tmp_path, [pin], pip_runner=pip, subject=_SUBJECT)

    archive = tmp_path / "artifacts" / f"{'ab' * 32}.tar.gz"
    assert archive.read_bytes() == b"archive"
    # Fetched once; the second build installs the archive already kept.
    assert fetches == ["AB" * 32]
    assert [call[-1] for call in pip.calls] == [str(archive), str(archive)]


async def test_a_github_pin_without_its_recorded_archive_raises(tmp_path: Path) -> None:
    with pytest.raises(PluginsDirError, match="github install with no recorded artifact and sha256"):
        await build_layer(tmp_path, [_pin("a", source="github")], pip_runner=LayingPip(), subject=_SUBJECT)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through directory permissions")
async def test_an_unwritable_plugins_directory_raises_naming_it(tmp_path: Path) -> None:
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(PluginsDirError, match=rf"plugins directory {re.escape(str(tmp_path))} is not writable"):
            await build_layer(tmp_path, [], pip_runner=LayingPip(), subject=_SUBJECT)
    finally:
        tmp_path.chmod(0o700)


# -- a server package's environment --------------------------------------------------------


async def test_a_server_build_is_a_venv_filled_by_pip_without_the_image_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIP_CONSTRAINT", "/image/constraints.txt")
    monkeypatch.setenv("PIP_INDEX_URL", "https://index.example/simple")
    pip = LayingPip()

    build = await build_server(tmp_path, _pin("srv"), servers=_SERVERS, pip_runner=pip, subject=_SERVER_SUBJECT)

    assert build.path == server_dir(tmp_path, "tai42-srv").build(build.build_id)
    python = build.path / "bin" / "python"
    assert python.exists()
    assert (build.path / "pyvenv.cfg").is_file()
    flags = ["--no-input", "--disable-pip-version-check"]
    assert pip.calls == [["--python", str(python), "install", *flags, "tai42-srv==1.0.0"]]
    # pip runs with this process's environment minus the image's constraint.
    env = pip.envs[0]
    assert env is not None
    assert "PIP_CONSTRAINT" not in env
    assert env["PIP_INDEX_URL"] == "https://index.example/simple"
    record = read_build_record(build.path)
    assert record is not None
    assert [item.label() for item in record.inputs] == ["tai42/srv 1.0.0"]


async def test_a_failed_server_build_deletes_its_directory(tmp_path: Path) -> None:
    with pytest.raises(PipFailedError):
        await build_server(
            tmp_path, _pin("srv"), servers=_SERVERS, pip_runner=LayingPip(fail="boom"), subject=_SERVER_SUBJECT
        )

    assert _builds(server_dir(tmp_path, "tai42-srv").root) == []


@pytest.mark.parametrize("command", [None, "python"], ids=["pip-fails", "undeclared-command"])
async def test_a_refused_first_server_build_leaves_no_server_directory(tmp_path: Path, command: str | None) -> None:
    # The store names no plugin in a directory a refused install leaves, so none is left.
    pip = LayingPip(fail="boom") if command is None else _server_pip()
    pin = _pin("srv") if command is None else _server_pin(command)
    with pytest.raises((PipFailedError, UndeclaredServerCommandError)):
        await build_server(tmp_path, pin, servers=_SERVERS, pip_runner=pip, subject=_SERVER_SUBJECT)

    assert not server_dir(tmp_path, "tai42-srv").root.exists()


async def test_a_refused_server_build_beside_a_built_environment_keeps_it(tmp_path: Path) -> None:
    built = await build_server(
        tmp_path, _server_pin("srv-mcp"), servers=_SERVERS, pip_runner=_server_pip(), subject=_SERVER_SUBJECT
    )

    with pytest.raises(UndeclaredServerCommandError):
        await build_server(
            tmp_path, _server_pin("python"), servers=_SERVERS, pip_runner=_server_pip(), subject=_SERVER_SUBJECT
        )

    assert _builds(server_dir(tmp_path, "tai42-srv").root) == [built.build_id]


def _server_pin(*commands: str) -> PluginPin:
    pin = _pin("srv")
    return PluginPin(
        input=pin.input,
        artifact_ref=pin.artifact_ref,
        server_commands=tuple(
            ServerCommand(item=f"srv_{index}", command=command) for index, command in enumerate(commands)
        ),
    )


def _server_pip() -> LayingPip:
    """pip laying the server package, declaring ``srv-mcp``, and a dependency declaring ``dep-tool``."""
    return LayingPip(
        server_dependencies=[("tai42-srv-dep", "1.0.0")],
        console_scripts={"tai42-srv": ["srv-mcp"], "tai42-srv-dep": ["dep-tool"]},
    )


async def test_a_server_build_whose_every_command_is_a_console_script_of_its_package_is_committed(
    tmp_path: Path,
) -> None:
    build = await build_server(
        tmp_path, _server_pin("srv-mcp"), servers=_SERVERS, pip_runner=_server_pip(), subject=_SERVER_SUBJECT
    )

    assert read_build_record(build.path) is not None
    assert _builds(server_dir(tmp_path, "tai42-srv").root) == [build.build_id]


@pytest.mark.parametrize("command", ["python", "dep-tool", "bin/srv-mcp"])
async def test_a_server_command_that_is_not_a_console_script_of_its_package_is_refused(
    tmp_path: Path, command: str
) -> None:
    with pytest.raises(UndeclaredServerCommandError) as caught:
        await build_server(
            tmp_path,
            _server_pin("srv-mcp", command),
            servers=_SERVERS,
            pip_runner=_server_pip(),
            subject=_SERVER_SUBJECT,
        )

    assert str(caught.value) == (
        f"tai42/srv: mcp-server item srv_1 names command {command!r}, which is not a console script of package "
        "tai42-srv (its scripts: srv-mcp); an mcp-server command is a console script the package declares"
    )
    assert _builds(server_dir(tmp_path, "tai42-srv").root) == []


# -- console scripts another server package exposes ---------------------------------------


def _other_server_pin(version: str = "1.0.0") -> PluginPin:
    return PluginPin(
        input=BuildInput(ref="tai42/other", package="tai42-other", version=version, source="pypi", sha256=None),
        artifact_ref=None,
        server_commands=(ServerCommand(item="other_0", command="other-mcp"),),
    )


async def _current_server(tmp_path: Path, pin: PluginPin, pip: LayingPip) -> str:
    build = await build_server(
        tmp_path, pin, servers=_SERVERS, pip_runner=pip, subject=f"cannot install {pin.input.label()}"
    )
    point_current(build.environment, build.build_id)
    return build.build_id


async def test_a_server_package_declaring_a_script_another_server_exposes_is_refused(tmp_path: Path) -> None:
    pip = LayingPip(console_scripts={"tai42-srv": ["srv-mcp"], "tai42-other": ["other-mcp", "srv-mcp"]})
    first = await _current_server(tmp_path, _server_pin("srv-mcp"), pip)

    with pytest.raises(ServerCommandCollisionError) as caught:
        await build_server(
            tmp_path, _other_server_pin(), servers=_SERVERS, pip_runner=pip, subject="cannot install tai42/other 1.0.0"
        )

    assert str(caught.value) == (
        "cannot install tai42/other 1.0.0: its package tai42-other declares the console script 'srv-mcp', which "
        "the installed server package tai42-srv already exposes; a bare mcp command names one package's script "
        "— uninstall the plugin that provides tai42-srv first, or choose a release that does not declare 'srv-mcp'"
    )
    assert not server_dir(tmp_path, "tai42-other").root.exists()
    assert _builds(server_dir(tmp_path, "tai42-srv").root) == [first]


async def test_updating_a_server_package_keeps_its_own_script_names(tmp_path: Path) -> None:
    pip = LayingPip(console_scripts={"tai42-other": ["other-mcp"]})
    await _current_server(tmp_path, _other_server_pin(), pip)

    build = await build_server(
        tmp_path,
        _other_server_pin("2.0.0"),
        servers=_SERVERS,
        pip_runner=pip,
        subject="cannot update tai42/other 2.0.0",
    )

    assert read_build_record(build.path) is not None
