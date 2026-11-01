"""Rebuilding the plugin environments that do not match the install store and this image.

The plugins directory is real; pip is :class:`LayingPip`, which lays the distributions a
case names into a layer build's ``--prefix``. A server build creates a real virtual
environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests._fakes.plugin_environment_dists import LayingPip
from tests.marketplace._specs import install_row, make_spec, mcp_server_item

from tai42_skeleton.marketplace.compat import declared_compat
from tai42_skeleton.marketplace.environments import rebuild as rebuild_module
from tai42_skeleton.marketplace.environments import swap as swap_module
from tai42_skeleton.marketplace.environments.layout import (
    BUILD_RECORD,
    layer_dir,
    read_build_record,
    server_dir,
)
from tai42_skeleton.marketplace.environments.rebuild import REBUILT, REMOVED, UP_TO_DATE, rebuild_environments
from tai42_skeleton.marketplace.environments.swap import current_build
from tai42_skeleton.marketplace.environments.verify import verify_environments
from tai42_skeleton.marketplace.errors import PipFailedError, PipUnavailableError

_TOOLBOX = make_spec(name="toolbox", package="tai42-fixture-toolbox")
_SERVER = make_spec(name="srv", package="tai42-fixture-srv", provides=[mcp_server_item()], contract=None)
_ALPHA = make_spec(
    name="alpha", package="tai42-fixture-alpha", provides=[mcp_server_item(name="a", command="a-mcp")], contract=None
)
_BETA = make_spec(
    name="beta", package="tai42-fixture-beta", provides=[mcp_server_item(name="b", command="b-mcp")], contract=None
)


@pytest.fixture
def pip_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record each pip-availability check the rebuild makes instead of probing this interpreter."""
    checks: list[str] = []
    monkeypatch.setattr(rebuild_module, "ensure_pip_available", lambda: checks.append("checked"))
    return checks


def _fixture_pip() -> LayingPip:
    """pip laying the toolbox into the layer and the server package, declaring its command, into its venv."""
    return LayingPip([("tai42-fixture-toolbox", "1.0.0", [])], console_scripts={"tai42-fixture-srv": ["srv-mcp"]})


def _builds(root: Path) -> list[str]:
    builds = root / "builds"
    return sorted(entry.name for entry in builds.iterdir()) if builds.is_dir() else []


async def test_an_empty_plugins_directory_is_rebuilt_from_the_store(tmp_path: Path, pip_checks: list[str]) -> None:
    rows = [install_row(_TOOLBOX), install_row(_SERVER)]
    pip = _fixture_pip()

    rebuild = await rebuild_environments(tmp_path, rows, pip_runner=pip)

    assert [(outcome.label, outcome.action) for outcome in rebuild.outcomes] == [
        ("layer", REBUILT),
        ("server tai42-fixture-srv", REBUILT),
    ]
    assert rebuild.outcomes[0].detail.startswith("plugin environment is out of date: the plugins directory")
    assert rebuild.layer_rebuilt_with_plugins is True
    assert pip_checks == ["checked"]
    # One layer pip call and one server pip call; afterwards everything verifies.
    assert [call[-1] for call in pip.calls] == ["tai42-fixture-toolbox==1.0.0", "tai42-fixture-srv==1.0.0"]
    assert verify_environments(tmp_path, rows).findings == []


async def test_a_rebuilt_layer_reports_the_plugins_it_left_out(tmp_path: Path, pip_checks: list[str]) -> None:
    # A contract range no running tai42-contract is inside, as a core upgrade leaves a store row.
    stranded = make_spec(name="stranded", package="tai42-fixture-stranded", contract=">=99,<100")
    rows = [install_row(_TOOLBOX), install_row(stranded)]
    pip = _fixture_pip()

    rebuild = await rebuild_environments(tmp_path, rows, pip_runner=pip)

    (layer,) = rebuild.outcomes
    assert layer.action == REBUILT
    reason = declared_compat("tai42-fixture-stranded", ">=99,<100").reason
    assert layer.detail.startswith("plugin environment is out of date: the plugins directory")
    assert layer.detail.endswith(f"; left out tai42/stranded 1.0.0: {reason}")
    assert [call[-1] for call in pip.calls] == ["tai42-fixture-toolbox==1.0.0"]
    assert verify_environments(tmp_path, rows).findings == []


async def test_a_matching_directory_is_left_as_it_is_with_no_pip(tmp_path: Path, pip_checks: list[str]) -> None:
    rows = [install_row(_TOOLBOX)]
    await rebuild_environments(tmp_path, rows, pip_runner=_fixture_pip())
    before = current_build(layer_dir(tmp_path))
    record = read_build_record(layer_dir(tmp_path).current)
    assert record is not None
    pip_checks.clear()
    pip = _fixture_pip()

    rebuild = await rebuild_environments(tmp_path, rows, pip_runner=pip)

    assert [(outcome.label, outcome.action, outcome.detail) for outcome in rebuild.outcomes] == [
        ("layer", UP_TO_DATE, f"1 plugins, built {record.built_at}")
    ]
    assert rebuild.layer_rebuilt_with_plugins is False
    assert pip.calls == []
    assert pip_checks == []
    assert current_build(layer_dir(tmp_path)) == before


async def test_a_rebuild_makes_the_new_build_current_and_prunes_the_old(tmp_path: Path, pip_checks: list[str]) -> None:
    await rebuild_environments(tmp_path, [install_row(_TOOLBOX)], pip_runner=_fixture_pip())
    old = current_build(layer_dir(tmp_path))

    rebuild = await rebuild_environments(tmp_path, [], pip_runner=LayingPip())

    rebuilt = current_build(layer_dir(tmp_path))
    assert rebuilt != old
    assert _builds(layer_dir(tmp_path).root) == [rebuilt]
    # The layer now serves no in-process plugin.
    assert [(outcome.label, outcome.action) for outcome in rebuild.outcomes] == [("layer", REBUILT)]
    assert rebuild.layer_rebuilt_with_plugins is False


async def test_a_layer_whose_current_record_is_unreadable_is_rebuilt(tmp_path: Path, pip_checks: list[str]) -> None:
    rows = [install_row(_TOOLBOX)]
    await rebuild_environments(tmp_path, rows, pip_runner=_fixture_pip())
    old = current_build(layer_dir(tmp_path))
    (layer_dir(tmp_path).current / BUILD_RECORD).write_text("{truncated", encoding="utf-8")

    rebuild = await rebuild_environments(tmp_path, rows, pip_runner=_fixture_pip())

    assert [(outcome.label, outcome.action) for outcome in rebuild.outcomes] == [("layer", REBUILT)]
    assert rebuild.outcomes[0].detail.startswith("plugin environment has an unreadable build record ")
    rebuilt = current_build(layer_dir(tmp_path))
    assert rebuilt != old
    assert _builds(layer_dir(tmp_path).root) == [rebuilt]
    assert verify_environments(tmp_path, rows).findings == []


async def test_a_server_package_whose_current_record_is_unreadable_is_rebuilt(
    tmp_path: Path, pip_checks: list[str]
) -> None:
    rows = [install_row(_SERVER)]
    await rebuild_environments(tmp_path, rows, pip_runner=_fixture_pip())
    environment = server_dir(tmp_path, "tai42-fixture-srv")
    old = current_build(environment)
    (environment.current / BUILD_RECORD).write_text("{truncated", encoding="utf-8")

    rebuild = await rebuild_environments(tmp_path, rows, pip_runner=_fixture_pip())

    assert [(outcome.label, outcome.action) for outcome in rebuild.outcomes] == [
        ("layer", UP_TO_DATE),
        ("server tai42-fixture-srv", REBUILT),
    ]
    assert current_build(environment) != old
    assert verify_environments(tmp_path, rows).findings == []


async def test_a_server_directory_the_store_does_not_name_with_an_unreadable_record_is_removed(
    tmp_path: Path, pip_checks: list[str]
) -> None:
    await rebuild_environments(tmp_path, [install_row(_SERVER)], pip_runner=_fixture_pip())
    (server_dir(tmp_path, "tai42-fixture-srv").current / BUILD_RECORD).write_text("{truncated", encoding="utf-8")

    rebuild = await rebuild_environments(tmp_path, [], pip_runner=LayingPip())

    assert (rebuild.outcomes[0].label, rebuild.outcomes[0].action) == ("server tai42-fixture-srv", REMOVED)
    assert not server_dir(tmp_path, "tai42-fixture-srv").root.exists()
    assert verify_environments(tmp_path, []).findings == []


async def test_an_empty_store_with_no_layer_build_needs_no_build(tmp_path: Path, pip_checks: list[str]) -> None:
    rebuild = await rebuild_environments(tmp_path, [], pip_runner=LayingPip())

    assert [(outcome.label, outcome.action, outcome.detail) for outcome in rebuild.outcomes] == [
        ("layer", UP_TO_DATE, "0 plugins, no build")
    ]
    assert pip_checks == []


async def test_a_server_directory_the_store_no_longer_names_is_removed(tmp_path: Path, pip_checks: list[str]) -> None:
    await rebuild_environments(tmp_path, [install_row(_SERVER)], pip_runner=_fixture_pip())

    rebuild = await rebuild_environments(tmp_path, [], pip_runner=LayingPip())

    assert (rebuild.outcomes[0].label, rebuild.outcomes[0].action, rebuild.outcomes[0].detail) == (
        "server tai42-fixture-srv",
        REMOVED,
        "the install store names no plugin in it",
    )
    assert not server_dir(tmp_path, "tai42-fixture-srv").root.exists()


async def test_a_build_failure_raises_and_leaves_the_current_build_in_place(
    tmp_path: Path, pip_checks: list[str]
) -> None:
    await rebuild_environments(tmp_path, [install_row(_TOOLBOX)], pip_runner=_fixture_pip())
    before = current_build(layer_dir(tmp_path))
    updated = [install_row(_TOOLBOX, version="2.0.0")]

    with pytest.raises(PipFailedError):
        await rebuild_environments(tmp_path, updated, pip_runner=LayingPip(fail="ERROR: no index"))

    assert current_build(layer_dir(tmp_path)) == before
    assert _builds(layer_dir(tmp_path).root) == [before]


async def test_a_stale_environment_without_pip_raises_before_any_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_pip() -> None:
        raise PipUnavailableError("no pip")

    monkeypatch.setattr(rebuild_module, "ensure_pip_available", no_pip)

    with pytest.raises(PipUnavailableError):
        await rebuild_environments(tmp_path, [install_row(_TOOLBOX)], pip_runner=_fixture_pip())

    assert not layer_dir(tmp_path).root.exists()


def _built_by_an_older_python(build: Path) -> None:
    """Make the server venv at ``build`` read as one an image with an older Python minor version built."""
    older = "3.11" if sys.version_info[:2] == (3, 12) else "3.12"
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    (site_root,) = (build / "lib").iterdir()
    site_root.rename(build / "lib" / site_root.name.replace(running, older))
    config = build / "pyvenv.cfg"
    config.write_text(config.read_text(encoding="utf-8").replace(running, older), encoding="utf-8")
    record = json.loads((build / BUILD_RECORD).read_text(encoding="utf-8"))
    record["python"] = older
    (build / BUILD_RECORD).write_text(json.dumps(record), encoding="utf-8")


async def test_after_a_python_upgrade_every_server_package_is_rebuilt(tmp_path: Path, pip_checks: list[str]) -> None:
    rows = [install_row(_ALPHA), install_row(_BETA)]
    pip = LayingPip(console_scripts={"tai42-fixture-alpha": ["a-mcp"], "tai42-fixture-beta": ["b-mcp"]})
    await rebuild_environments(tmp_path, rows, pip_runner=pip)
    for package in ("tai42-fixture-alpha", "tai42-fixture-beta"):
        environment = server_dir(tmp_path, package)
        _built_by_an_older_python(environment.build(str(current_build(environment))))
    stale = [status.label for status in verify_environments(tmp_path, rows).environments if status.finding is not None]
    assert stale == ["server tai42-fixture-alpha", "server tai42-fixture-beta"]

    rebuild = await rebuild_environments(tmp_path, rows, pip_runner=pip)

    assert [(outcome.label, outcome.action) for outcome in rebuild.outcomes] == [
        ("layer", UP_TO_DATE),
        ("server tai42-fixture-alpha", REBUILT),
        ("server tai42-fixture-beta", REBUILT),
    ]
    assert verify_environments(tmp_path, rows).findings == []


async def test_a_rebuild_judges_command_collisions_against_no_build_it_replaces(
    tmp_path: Path, pip_checks: list[str]
) -> None:
    # beta 1.0.0 declares x-mcp; the store moves beta to 2.0.0, which does not, and alpha to 2.0.0, which does.
    first = LayingPip(console_scripts={"tai42-fixture-alpha": ["a-mcp"], "tai42-fixture-beta": ["b-mcp", "x-mcp"]})
    await rebuild_environments(tmp_path, [install_row(_ALPHA), install_row(_BETA)], pip_runner=first)
    rows = [install_row(_ALPHA, version="2.0.0"), install_row(_BETA, version="2.0.0")]
    pip = LayingPip(console_scripts={"tai42-fixture-alpha": ["a-mcp", "x-mcp"], "tai42-fixture-beta": ["b-mcp"]})

    rebuild = await rebuild_environments(tmp_path, rows, pip_runner=pip)

    assert [(outcome.label, outcome.action) for outcome in rebuild.outcomes] == [
        ("layer", UP_TO_DATE),
        ("server tai42-fixture-alpha", REBUILT),
        ("server tai42-fixture-beta", REBUILT),
    ]
    assert verify_environments(tmp_path, rows).findings == []


async def test_a_rebuild_withdraws_a_left_over_server_s_current_before_deleting_it(
    tmp_path: Path, pip_checks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await rebuild_environments(tmp_path, [install_row(_SERVER)], pip_runner=_fixture_pip())
    environment = server_dir(tmp_path, "tai42-fixture-srv")

    def refuse(path: Path) -> None:
        raise OSError(f"cannot delete {path}")

    monkeypatch.setattr(swap_module.shutil, "rmtree", refuse)

    with pytest.raises(OSError, match="cannot delete"):
        await rebuild_environments(tmp_path, [], pip_runner=LayingPip())

    # The deletion failed, yet the left-over server no longer has a ``current`` to expose commands from.
    assert current_build(environment) is None
