"""The console scripts of server builds and the server package each script name belongs to.

Every case lays real ``current`` server builds under a temporary plugins directory, each with
its package's ``.dist-info`` and executable scripts in ``bin``, and names the server packages
to read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests._fakes.plugin_environment_dists import install_fake_server_dist

from tai42_skeleton.marketplace.environments.console_scripts import ScriptOwner, server_script_owners
from tai42_skeleton.marketplace.environments.layout import server_dir
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.errors import PluginsDirError


def _current_server(plugins_dir: Path, package: str, *scripts: str) -> Path:
    environment = server_dir(plugins_dir, package)
    install_fake_server_dist(environment.build("s1"), package, "1.0.0", console_scripts=scripts)
    point_current(environment, "s1")
    return environment.current


def test_every_script_a_current_server_build_declares_is_owned_by_its_package(tmp_path: Path) -> None:
    first = _current_server(tmp_path, "tai42-fixture-a", "a-mcp", "a-admin")
    second = _current_server(tmp_path, "tai42-fixture-b", "b-mcp")

    assert server_script_owners(tmp_path, ["tai42-fixture-a", "tai42-fixture-b"]) == {
        "a-admin": ScriptOwner(package="tai42-fixture-a", path=first / "bin" / "a-admin"),
        "a-mcp": ScriptOwner(package="tai42-fixture-a", path=first / "bin" / "a-mcp"),
        "b-mcp": ScriptOwner(package="tai42-fixture-b", path=second / "bin" / "b-mcp"),
    }


def test_only_the_named_packages_scripts_are_read_by_any_spelling(tmp_path: Path) -> None:
    _current_server(tmp_path, "tai42-fixture-a", "a-mcp")
    _current_server(tmp_path, "tai42-fixture-b", "b-mcp")

    assert set(server_script_owners(tmp_path, ["Tai42_Fixture.B"])) == {"b-mcp"}


def test_a_named_package_with_no_current_build_owns_no_script(tmp_path: Path) -> None:
    assert server_script_owners(tmp_path, ["tai42-fixture-a"]) == {}


def test_one_script_two_server_packages_declare_raises_naming_both(tmp_path: Path) -> None:
    _current_server(tmp_path, "tai42-fixture-a", "shared-mcp")
    _current_server(tmp_path, "tai42-fixture-b", "shared-mcp")

    with pytest.raises(PluginsDirError) as caught:
        server_script_owners(tmp_path, ["tai42-fixture-a", "tai42-fixture-b"])

    assert str(caught.value) == (
        "server packages tai42-fixture-a and tai42-fixture-b both declare the console script 'shared-mcp'; "
        "a bare mcp command names one package's script — uninstall one of them"
    )
