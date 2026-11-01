"""One marketplace operation's environment changes, across the plugin layer and the server environments.

The plugins directory is real; pip is :class:`LayingPip`. Each case moves one plugin from an
old pin to a new one (``None`` for an install or an uninstall) and checks which
environments are built, made current, restored and deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests._fakes.plugin_environment_dists import LayingPip, current_layer_build, write_dist_info

from tai42_skeleton.marketplace.compat import declared_compat
from tai42_skeleton.marketplace.environments.change import compiled_module_notes, prepare_change
from tai42_skeleton.marketplace.environments.layout import BuildInput, layer_dir, layer_site_dirs, server_dir
from tai42_skeleton.marketplace.environments.swap import current_build
from tai42_skeleton.marketplace.environments.targets import PlacedPin, PluginPin, targets_of
from tai42_skeleton.marketplace.errors import PipFailedError

_SUBJECT = "cannot install tai42/toolbox 1.0.0"


def _placed(package: str, version: str = "1.0.0", *, server: bool = False) -> PlacedPin:
    item = BuildInput(ref="tai42/toolbox", package=package, version=version, source="pypi", sha256=None)
    return PlacedPin(pin=PluginPin(input=item, artifact_ref=None), runs_as_server=server)


def _builds(root: Path) -> list[str]:
    builds = root / "builds"
    return sorted(entry.name for entry in builds.iterdir()) if builds.is_dir() else []


async def _change(plugins_dir: Path, old: PlacedPin | None, new: PlacedPin | None, pip: LayingPip | None = None):
    return await prepare_change(
        plugins_dir, targets_of([new]), old, new, pip_runner=pip or LayingPip(), subject=_SUBJECT
    )


async def test_installing_an_in_process_plugin_rebuilds_only_the_layer(tmp_path: Path) -> None:
    pip = LayingPip()

    change = await _change(tmp_path, None, _placed("tai42-fixture-toolbox"), pip)

    assert [(item.environment, item.is_layer) for item in change.changes] == [(layer_dir(tmp_path), True)]
    assert pip.calls[0][-1] == "tai42-fixture-toolbox==1.0.0"


async def test_an_install_rebuilding_the_layer_beside_a_plugin_it_leaves_out_says_so(tmp_path: Path) -> None:
    # A contract range no running tai42-contract is inside, as a core upgrade leaves a store row.
    stranded = BuildInput(ref="tai42/stranded", package="tai42-stranded", version="1.0.0", source="pypi", sha256=None)
    left_out = PlacedPin(PluginPin(input=stranded, artifact_ref=None, contract=">=99,<100"), runs_as_server=False)
    new = _placed("tai42-fixture-toolbox")

    change = await prepare_change(
        tmp_path, targets_of([new, left_out]), None, new, pip_runner=LayingPip(), subject=_SUBJECT
    )
    change.swap()

    reason = declared_compat("tai42-stranded", ">=99,<100").reason
    assert change.finish() == [f"left out tai42/stranded 1.0.0: {reason}"]


async def test_installing_a_server_package_builds_only_its_environment(tmp_path: Path) -> None:
    change = await _change(tmp_path, None, _placed("tai42-fixture-srv", server=True))

    assert [item.environment for item in change.changes] == [server_dir(tmp_path, "tai42-fixture-srv")]
    assert not layer_dir(tmp_path).root.exists()


async def test_an_update_from_the_layer_to_a_server_rebuilds_the_layer_and_builds_the_server(tmp_path: Path) -> None:
    pip = LayingPip()

    change = await _change(
        tmp_path, _placed("tai42-fixture-pkg"), _placed("tai42-fixture-pkg", "2.0.0", server=True), pip
    )

    assert [(item.environment, item.build is not None) for item in change.changes] == [
        (layer_dir(tmp_path), True),
        (server_dir(tmp_path, "tai42-fixture-pkg"), True),
    ]
    # The layer is rebuilt without the plugin: an empty layer, no pip call for it.
    assert [call[-1] for call in pip.calls] == ["tai42-fixture-pkg==2.0.0"]


async def test_an_update_from_a_server_to_the_layer_removes_the_server_environment(tmp_path: Path) -> None:
    (await _change(tmp_path, None, _placed("tai42-fixture-pkg", server=True))).swap()

    change = await _change(tmp_path, _placed("tai42-fixture-pkg", server=True), _placed("tai42-fixture-pkg", "2.0.0"))

    assert [(item.environment, item.build is not None) for item in change.changes] == [
        (layer_dir(tmp_path), True),
        (server_dir(tmp_path, "tai42-fixture-pkg"), False),
    ]


async def test_a_server_update_that_renames_its_package_moves_to_a_new_environment(tmp_path: Path) -> None:
    (await _change(tmp_path, None, _placed("tai42-fixture-old", server=True))).swap()

    change = await _change(
        tmp_path, _placed("tai42-fixture-old", server=True), _placed("tai42-fixture-new", "2.0.0", server=True)
    )

    assert [(item.environment, item.build is not None) for item in change.changes] == [
        (server_dir(tmp_path, "tai42-fixture-old"), False),
        (server_dir(tmp_path, "tai42-fixture-new"), True),
    ]


async def test_a_server_update_in_place_builds_beside_the_build_in_use(tmp_path: Path) -> None:
    installed = await _change(tmp_path, None, _placed("tai42-fixture-srv", server=True))
    installed.swap()
    installed.finish()
    first = current_build(server_dir(tmp_path, "tai42-fixture-srv"))

    change = await _change(
        tmp_path, _placed("tai42-fixture-srv", server=True), _placed("tai42-fixture-srv", "2.0.0", server=True)
    )
    change.swap()

    environment = server_dir(tmp_path, "tai42-fixture-srv")
    assert [item.environment for item in change.changes] == [environment]
    assert current_build(environment) != first
    assert change.finish() == []
    assert _builds(environment.root) == [current_build(environment)]


async def test_uninstalling_a_server_package_withdraws_its_build_and_deletes_its_directory_once_finished(
    tmp_path: Path,
) -> None:
    installed = await _change(tmp_path, None, _placed("tai42-fixture-srv", server=True))
    installed.swap()
    environment = server_dir(tmp_path, "tai42-fixture-srv")
    pip = LayingPip()

    change = await _change(tmp_path, _placed("tai42-fixture-srv", server=True), None, pip)
    change.swap()

    assert pip.calls == []
    assert current_build(environment) is None
    assert environment.root.exists()
    change.finish()
    assert not environment.root.exists()


async def test_swapping_back_a_server_uninstall_restores_its_build(tmp_path: Path) -> None:
    installed = await _change(tmp_path, None, _placed("tai42-fixture-srv", server=True))
    installed.swap()
    environment = server_dir(tmp_path, "tai42-fixture-srv")
    before = current_build(environment)

    change = await _change(tmp_path, _placed("tai42-fixture-srv", server=True), None)
    change.swap()
    change.swap_back()

    assert change.restores_builds is True
    assert before is not None
    assert current_build(environment) == before
    assert _builds(environment.root) == [before]


async def test_every_operation_withdraws_and_then_deletes_a_server_directory_the_store_does_not_name(
    tmp_path: Path,
) -> None:
    # A server directory left by an uninstall that could not delete it, or that stopped before it did.
    left = await _change(tmp_path, None, _placed("tai42-fixture-left", server=True))
    left.swap()
    environment = server_dir(tmp_path, "tai42-fixture-left")
    before = current_build(environment)

    change = await _change(tmp_path, None, _placed("tai42-fixture-toolbox"))
    change.swap()
    assert current_build(environment) is None
    change.swap_back()
    assert current_build(environment) == before

    change = await _change(tmp_path, None, _placed("tai42-fixture-toolbox"))
    change.swap()
    change.finish()
    assert not environment.root.exists()


async def test_a_server_directory_the_store_does_not_name_never_blocks_a_package_declaring_its_script(
    tmp_path: Path,
) -> None:
    pip = LayingPip(console_scripts={"tai42-fixture-left": ["shared-mcp"], "tai42-fixture-srv": ["shared-mcp"]})
    left = await _change(tmp_path, None, _placed("tai42-fixture-left", server=True), pip)
    left.swap()

    change = await _change(tmp_path, None, _placed("tai42-fixture-srv", server=True), pip)
    change.swap()

    assert current_build(server_dir(tmp_path, "tai42-fixture-left")) is None
    assert current_build(server_dir(tmp_path, "tai42-fixture-srv")) is not None


async def test_a_server_update_that_renames_its_package_may_keep_its_script_name(tmp_path: Path) -> None:
    pip = LayingPip(console_scripts={"tai42-fixture-old": ["srv-mcp"], "tai42-fixture-new": ["srv-mcp"]})
    installed = await _change(tmp_path, None, _placed("tai42-fixture-old", server=True), pip)
    installed.swap()
    installed.finish()

    change = await _change(
        tmp_path, _placed("tai42-fixture-old", server=True), _placed("tai42-fixture-new", "2.0.0", server=True), pip
    )
    change.swap()
    change.finish()

    assert not server_dir(tmp_path, "tai42-fixture-old").root.exists()
    assert current_build(server_dir(tmp_path, "tai42-fixture-new")) is not None


async def test_swapping_back_a_first_server_install_deletes_its_whole_directory(tmp_path: Path) -> None:
    change = await _change(tmp_path, None, _placed("tai42-fixture-srv", server=True))
    change.swap()
    change.swap_back()

    assert not server_dir(tmp_path, "tai42-fixture-srv").root.exists()


async def test_swapping_back_restores_the_build_in_use_before_and_deletes_the_new_one(tmp_path: Path) -> None:
    first = await _change(tmp_path, None, _placed("tai42-fixture-toolbox"))
    first.swap()
    first.finish()
    before = current_build(layer_dir(tmp_path))

    change = await _change(tmp_path, _placed("tai42-fixture-toolbox"), _placed("tai42-fixture-toolbox", "2.0.0"))
    change.swap()
    assert current_build(layer_dir(tmp_path)) != before
    change.swap_back()

    assert change.restores_builds is True
    assert current_build(layer_dir(tmp_path)) == before
    assert _builds(layer_dir(tmp_path).root) == [before]


async def test_a_failed_server_build_deletes_the_layer_build_the_operation_made(tmp_path: Path) -> None:
    first = await _change(tmp_path, None, _placed("tai42-fixture-pkg"))
    first.swap()
    first.finish()
    before = current_build(layer_dir(tmp_path))

    with pytest.raises(PipFailedError):
        await _change(
            tmp_path,
            _placed("tai42-fixture-pkg"),
            _placed("tai42-fixture-pkg", "2.0.0", server=True),
            LayingPip(fail="ERROR: no index"),
        )

    assert _builds(layer_dir(tmp_path).root) == [before]
    assert _builds(server_dir(tmp_path, "tai42-fixture-pkg").root) == []


# -- compiled modules a running process keeps ------------------------------------------------


def _compiled(build: Path, package: str, version: str) -> None:
    """Lay ``package`` with a compiled extension module its ``RECORD`` lists (metadata lists only existing files)."""
    site = layer_site_dirs(build)[0]
    info = write_dist_info(site, package, version)
    extension = f"{package}/_speedups.cpython-313-x86_64-linux-gnu.so"
    (site / package).mkdir()
    (site / extension).write_bytes(b"")
    (info / "RECORD").write_text(f"{extension},,\n", encoding="utf-8")


def test_a_changed_distribution_with_compiled_modules_is_noted(tmp_path: Path) -> None:
    old = current_layer_build(tmp_path, "old")
    new = current_layer_build(tmp_path, "new")
    _compiled(old, "tai42-fixture-fast", "1.0.0")
    _compiled(new, "tai42-fixture-fast", "2.0.0")
    write_dist_info(layer_site_dirs(old)[0], "tai42-fixture-pure", "1.0.0")
    write_dist_info(layer_site_dirs(new)[0], "tai42-fixture-pure", "2.0.0")

    assert compiled_module_notes(old, new) == [
        "tai42-fixture-fast 1.0.0 -> 2.0.0 has compiled modules that a running process cannot replace "
        "— restart the server to load it"
    ]


def test_an_unchanged_compiled_distribution_is_not_noted(tmp_path: Path) -> None:
    old = current_layer_build(tmp_path, "old")
    new = current_layer_build(tmp_path, "new")
    _compiled(old, "tai42-fixture-fast", "1.0.0")
    _compiled(new, "tai42-fixture-fast", "1.0.0")

    assert compiled_module_notes(old, new) == []
