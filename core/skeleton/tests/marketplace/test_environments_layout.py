"""The plugins directory's layout, the build record, and the ``current`` swap.

Every case runs against a real temporary plugins directory: the record is written and read
back from disk, and ``current`` is a real relative symlink.
"""

from __future__ import annotations

import json
import os
import re
import sys
import sysconfig
from pathlib import Path

import pytest

from tai42_skeleton.marketplace.environments import swap as swap_module
from tai42_skeleton.marketplace.environments.layout import (
    BUILD_RECORD,
    BuildInput,
    EnvironmentDir,
    Excluded,
    artifacts_dir,
    layer_dir,
    layer_site_dirs,
    new_build_id,
    read_build_record,
    server_dir,
    server_site_dirs,
    write_build_record,
)
from tai42_skeleton.marketplace.environments.swap import (
    current_build,
    point_current,
    prune,
    remove_environment,
    swap_back,
)
from tai42_skeleton.marketplace.errors import PluginsDirError


def _input(ref: str = "tai42/toolbox", version: str = "1.0.0", sha256: str | None = None) -> BuildInput:
    return BuildInput(ref=ref, package=ref.replace("/", "-"), version=version, source="pypi", sha256=sha256)


def _build(environment: EnvironmentDir, build_id: str) -> Path:
    build = environment.build(build_id)
    build.mkdir(parents=True)
    return build


# -- layout ----------------------------------------------------------------------


def test_each_environment_lives_at_its_place_under_the_plugins_directory(tmp_path: Path) -> None:
    assert layer_dir(tmp_path).root == tmp_path / "layer"
    assert layer_dir(tmp_path).current == tmp_path / "layer" / "current"
    assert layer_dir(tmp_path).build("b1") == tmp_path / "layer" / "builds" / "b1"
    assert layer_dir(tmp_path).tmp_build("b1") == tmp_path / "layer" / "builds" / ".tmp-b1"
    # A server package's directory is its normalized name.
    assert server_dir(tmp_path, "Tai42_Srv.Pkg").root == tmp_path / "servers" / "tai42-srv-pkg"
    assert artifacts_dir(tmp_path) == tmp_path / "artifacts"


def test_the_layer_site_dirs_are_the_prefix_scheme_under_the_build(tmp_path: Path) -> None:
    expected = sysconfig.get_paths(vars={"base": str(tmp_path), "platbase": str(tmp_path)})
    dirs = layer_site_dirs(tmp_path)
    assert dirs[0] == Path(expected["purelib"])
    assert set(dirs) == {Path(expected["purelib"]), Path(expected["platlib"])}
    assert all(tmp_path in directory.parents for directory in dirs)


def _another_python() -> str:
    """A ``major.minor`` the running interpreter is not, as a venv built by an older image records."""
    return "3.11" if sys.version_info[:2] == (3, 12) else "3.12"


@pytest.mark.parametrize("line", ["version = {python}.4", "version_info = {python}"])
def test_a_server_venv_s_site_dirs_are_named_by_the_python_its_own_config_records(tmp_path: Path, line: str) -> None:
    python = _another_python()
    (tmp_path / "pyvenv.cfg").write_text(f"home = /usr/bin\n{line.format(python=python)}\n", encoding="utf-8")

    dirs = server_site_dirs(tmp_path)

    abi_thread = sysconfig.get_config_var("abi_thread") or ""
    assert dirs[0] == tmp_path / "lib" / f"python{python}{abi_thread}" / "site-packages"
    assert all(directory.name == "site-packages" and f"python{python}" in directory.parent.name for directory in dirs)


@pytest.mark.parametrize("config", [None, "home = /usr/bin\n"])
def test_a_server_venv_recording_no_python_version_raises_naming_its_config(tmp_path: Path, config: str | None) -> None:
    if config is not None:
        (tmp_path / "pyvenv.cfg").write_text(config, encoding="utf-8")

    with pytest.raises(
        PluginsDirError,
        match=(
            rf"^{re.escape(str(tmp_path / 'pyvenv.cfg'))} records no Python version, so the site directories of "
            r"the server build are unknown; run 'tai plugins rebuild' against this image$"
        ),
    ):
        server_site_dirs(tmp_path)


def test_a_build_id_is_the_utc_second_then_six_hex_characters() -> None:
    first, second = new_build_id(), new_build_id()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", first)
    assert first != second


# -- the build record ---------------------------------------------------------------


def test_the_build_record_round_trips_its_sorted_inputs_and_the_interpreter(tmp_path: Path) -> None:
    inputs = [_input("tai42/zeta", "2.0.0"), _input("tai42/alpha", "1.0.0", sha256="a" * 64)]

    excluded = [
        Excluded(input=_input("tai42/omega", "3.0.0"), modules=("omega.tools",), reason="omega requires ..."),
        Excluded(input=_input("tai42/beta", "1.0.0"), modules=(), reason="beta requires ..."),
    ]

    written = write_build_record(tmp_path, inputs, excluded)
    read = read_build_record(tmp_path)

    assert read is not None
    assert read == written
    assert [item.ref for item in read.inputs] == ["tai42/alpha", "tai42/zeta"]
    assert [item.input.ref for item in read.excluded] == ["tai42/beta", "tai42/omega"]
    assert read.excluded[1] == excluded[0]
    assert read.python == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert read.platform == sysconfig.get_platform()


def test_a_build_without_a_record_reads_as_incomplete(tmp_path: Path) -> None:
    assert read_build_record(tmp_path) is None


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        json.dumps({"inputs": [{"ref": "x"}]}),
        json.dumps([1]),
        json.dumps({"inputs": [], "excluded": [{"ref": "x"}], "python": "3", "platform": "p", "built_at": "t"}),
        json.dumps({"inputs": [], "python": "3", "platform": "p", "built_at": "t"}),
    ],
)
def test_an_unreadable_record_raises_naming_the_file(tmp_path: Path, document: str) -> None:
    (tmp_path / BUILD_RECORD).write_text(document, encoding="utf-8")

    with pytest.raises(PluginsDirError, match=re.escape(str(tmp_path / BUILD_RECORD))):
        read_build_record(tmp_path)


# -- current, swap back and prune ----------------------------------------------------


def test_current_is_a_relative_symlink_into_builds(tmp_path: Path) -> None:
    environment = layer_dir(tmp_path)
    _build(environment, "b1")

    point_current(environment, "b1")

    assert os.readlink(environment.current) == os.path.join("builds", "b1")
    assert current_build(environment) == "b1"


def test_pointing_current_replaces_the_link_and_leaves_no_staged_link(tmp_path: Path) -> None:
    environment = layer_dir(tmp_path)
    _build(environment, "b1")
    _build(environment, "b2")
    point_current(environment, "b1")

    point_current(environment, "b2")

    assert current_build(environment) == "b2"
    assert not (environment.root / "current.new").is_symlink()


def test_an_environment_without_current_has_no_current_build(tmp_path: Path) -> None:
    assert current_build(layer_dir(tmp_path)) is None


def test_a_current_that_is_not_a_symlink_raises(tmp_path: Path) -> None:
    environment = layer_dir(tmp_path)
    environment.current.mkdir(parents=True)

    with pytest.raises(PluginsDirError, match="is not a symlink to a build"):
        current_build(environment)


def test_a_current_pointing_outside_builds_raises(tmp_path: Path) -> None:
    environment = layer_dir(tmp_path)
    environment.root.mkdir(parents=True)
    os.symlink(tmp_path / "elsewhere", environment.current)

    with pytest.raises(PluginsDirError, match="not at a build under builds/"):
        current_build(environment)


def test_swap_back_restores_the_previous_build_and_deletes_the_failed_one(tmp_path: Path) -> None:
    environment = layer_dir(tmp_path)
    _build(environment, "old")
    _build(environment, "new")
    point_current(environment, "new")

    swap_back(environment, "old", "new")

    assert current_build(environment) == "old"
    assert not environment.build("new").exists()


def test_swap_back_with_no_previous_build_removes_current(tmp_path: Path) -> None:
    environment = layer_dir(tmp_path)
    _build(environment, "new")
    point_current(environment, "new")

    swap_back(environment, None, "new")

    assert not environment.current.is_symlink()
    assert not environment.build("new").exists()


def test_prune_keeps_only_the_current_build(tmp_path: Path) -> None:
    environment = layer_dir(tmp_path)
    for build_id in ("old", "keep", ".tmp-interrupted"):
        _build(environment, build_id)
    point_current(environment, "keep")

    removed = prune(environment)

    assert sorted(path.name for path in removed) == [".tmp-interrupted", "old"]
    assert [entry.name for entry in environment.builds.iterdir()] == ["keep"]


def test_remove_environment_deletes_the_whole_directory(tmp_path: Path) -> None:
    environment = server_dir(tmp_path, "tai42-srv")
    _build(environment, "b1")
    point_current(environment, "b1")

    remove_environment(environment)

    assert not environment.root.exists()


def test_remove_environment_withdraws_current_before_deleting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = server_dir(tmp_path, "tai42-srv")
    _build(environment, "b1")
    point_current(environment, "b1")

    def refuse(path: Path) -> None:
        raise OSError(f"cannot delete {path}")

    monkeypatch.setattr(swap_module.shutil, "rmtree", refuse)

    with pytest.raises(OSError, match="cannot delete"):
        remove_environment(environment)

    # A deletion that fails part-way leaves no ``current`` serving a half-deleted build.
    assert current_build(environment) is None
    assert environment.build("b1").is_dir()
