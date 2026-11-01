"""Making the current plugin environments visible to this process.

The plugins directory is real: layer builds hold a small importable package, and server
builds hold their package's distribution metadata and executable console scripts.
``sys.path``, the activated build, the declared stdio launch provider and the fixture modules
are restored after every case.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_kit.transport import resolve_stdio_command
from tai42_kit.transport import stdio_launch as stdio_launch_module
from tests._fakes.plugin_environment_dists import (
    LayingPip,
    current_layer_build,
    install_fake_dist,
    install_fake_server_dist,
)

from tai42_skeleton.marketplace.environments import activate as activate_module
from tai42_skeleton.marketplace.environments.activate import (
    activate_plugins_dir,
    drop_layer_modules,
    exposed_server_commands,
)
from tai42_skeleton.marketplace.environments.change import prepare_change
from tai42_skeleton.marketplace.environments.layout import BuildInput, layer_dir, layer_site_dirs, server_dir
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.environments.targets import PlacedPin, PluginPin, targets_of
from tai42_skeleton.marketplace.errors import PluginsDirError

_MODULE = "tai42_fixture_layered"


@pytest.fixture
def plugins_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A configured plugins directory, with this process's import state restored afterwards."""
    directory = tmp_path / "plugins"
    monkeypatch.setattr(activate_module, "configured_plugins_dir", lambda: directory)
    monkeypatch.setattr(activate_module._ACTIVE, "target", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(stdio_launch_module, "_LAUNCH", stdio_launch_module._no_exposed_commands)
    yield directory
    sys.modules.pop(_MODULE, None)


def _layer_build(plugins_dir: Path, build_id: str, value: int) -> Path:
    build = layer_dir(plugins_dir).build(build_id)
    site = install_fake_dist(build, _MODULE, "1.0.0")
    (site / _MODULE / "__init__.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    return build


def _server_build(
    plugins_dir: Path,
    package: str,
    *commands: str,
    dependency_commands: tuple[str, ...] = (),
    python: str | None = None,
) -> Path:
    """A ``current`` server build of ``package`` declaring ``commands``, beside a dependency declaring its own.

    The build is a venv of Python ``python``, by default the running interpreter's version.
    """
    environment = server_dir(plugins_dir, package)
    build = environment.build("s1")
    install_fake_server_dist(build, package, "1.0.0", console_scripts=commands, python=python)
    install_fake_server_dist(build, f"{package}-dep", "1.0.0", console_scripts=dependency_commands)
    (build / "bin" / "python").symlink_to(sys.executable)
    point_current(environment, "s1")
    return environment.current / "bin"


def test_the_current_layer_build_is_put_on_sys_path_through_current(plugins_dir: Path) -> None:
    _layer_build(plugins_dir, "b1", 1)
    point_current(layer_dir(plugins_dir), "b1")

    activate_plugins_dir()

    site_dir = str(layer_site_dirs(layer_dir(plugins_dir).current)[0])
    assert sys.path[-1] == site_dir
    assert importlib.import_module(_MODULE).VALUE == 1


def test_activating_the_same_build_again_keeps_the_loaded_modules(plugins_dir: Path) -> None:
    _layer_build(plugins_dir, "b1", 1)
    point_current(layer_dir(plugins_dir), "b1")
    activate_plugins_dir()
    module = importlib.import_module(_MODULE)
    path_before = list(sys.path)

    activate_plugins_dir()

    assert sys.modules[_MODULE] is module
    assert sys.path == path_before


def test_a_new_current_build_drops_the_layer_modules_so_they_import_from_it(plugins_dir: Path) -> None:
    _layer_build(plugins_dir, "b1", 1)
    point_current(layer_dir(plugins_dir), "b1")
    activate_plugins_dir()
    assert importlib.import_module(_MODULE).VALUE == 1
    _layer_build(plugins_dir, "b2", 2)
    point_current(layer_dir(plugins_dir), "b2")

    activate_plugins_dir()

    assert _MODULE not in sys.modules
    assert importlib.import_module(_MODULE).VALUE == 2


def test_dropping_the_layer_modules_leaves_every_other_module(plugins_dir: Path) -> None:
    build = current_layer_build(plugins_dir)
    install_fake_dist(build, _MODULE, "1.0.0")
    sys.path.append(str(layer_site_dirs(layer_dir(plugins_dir).current)[0]))
    importlib.import_module(_MODULE)

    assert drop_layer_modules(layer_dir(plugins_dir).root) == 1
    assert _MODULE not in sys.modules
    assert "pytest" in sys.modules


def test_with_no_layer_build_nothing_is_put_on_sys_path(plugins_dir: Path) -> None:
    path_before = list(sys.path)

    activate_plugins_dir()

    assert sys.path == path_before


def test_with_no_plugins_directory_no_server_command_is_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activate_module, "configured_plugins_dir", lambda: None)

    assert exposed_server_commands() == {}


def test_a_server_build_exposes_only_its_own_package_s_console_scripts(plugins_dir: Path) -> None:
    a_bin = _server_build(plugins_dir, "tai42-fixture-a", "a-mcp", dependency_commands=("dep-tool",))
    b_bin = _server_build(plugins_dir, "tai42-fixture-b", "b-mcp", "b-admin")
    # A server directory with no current build exposes nothing.
    server_dir(plugins_dir, "tai42-fixture-c").builds.mkdir(parents=True)

    # Neither the venv's interpreter nor a dependency's script is exposed.
    assert exposed_server_commands() == {
        "a-mcp": str(a_bin / "a-mcp"),
        "b-admin": str(b_bin / "b-admin"),
        "b-mcp": str(b_bin / "b-mcp"),
    }


def test_a_server_build_an_older_python_made_exposes_its_commands_from_its_own_site_dir(plugins_dir: Path) -> None:
    older = "3.11" if sys.version_info[:2] == (3, 12) else "3.12"
    a_bin = _server_build(plugins_dir, "tai42-fixture-a", "a-mcp", python=older)
    b_bin = _server_build(plugins_dir, "tai42-fixture-b", "b-mcp")

    assert exposed_server_commands() == {"a-mcp": str(a_bin / "a-mcp"), "b-mcp": str(b_bin / "b-mcp")}


async def test_a_server_directory_the_store_does_not_name_exposes_no_command_once_an_operation_ran(
    plugins_dir: Path,
) -> None:
    _server_build(plugins_dir, "tai42-fixture-left", "left-mcp")
    kept = _server_build(plugins_dir, "tai42-fixture-kept", "kept-mcp")
    server = BuildInput(ref="tai42/kept", package="tai42-fixture-kept", version="1.0.0", source="pypi", sha256=None)
    toolbox = BuildInput(ref="tai42/tb", package="tai42-fixture-tb", version="1.0.0", source="pypi", sha256=None)
    new = PlacedPin(pin=PluginPin(input=toolbox, artifact_ref=None), runs_as_server=False)
    targets = targets_of([PlacedPin(pin=PluginPin(input=server, artifact_ref=None), runs_as_server=True), new])

    change = await prepare_change(plugins_dir, targets, None, new, pip_runner=LayingPip(), subject="cannot install")
    change.swap()

    assert exposed_server_commands() == {"kept-mcp": str(kept / "kept-mcp")}


def test_a_current_server_build_without_its_package_s_distribution_raises(plugins_dir: Path) -> None:
    environment = server_dir(plugins_dir, "tai42-fixture-a")
    install_fake_server_dist(environment.build("s1"), "tai42-fixture-other", "1.0.0", console_scripts=("a-mcp",))
    point_current(environment, "s1")

    with pytest.raises(PluginsDirError) as caught:
        exposed_server_commands()

    message = str(caught.value)
    assert "tai42-fixture-a" in message
    assert str(environment.current) in message


def test_one_command_declared_by_two_server_packages_raises_naming_both(plugins_dir: Path) -> None:
    _server_build(plugins_dir, "tai42-fixture-a", "shared-mcp")
    _server_build(plugins_dir, "tai42-fixture-b", "shared-mcp")

    with pytest.raises(PluginsDirError) as caught:
        exposed_server_commands()

    message = str(caught.value)
    assert "'shared-mcp'" in message
    assert "tai42-fixture-a" in message
    assert "tai42-fixture-b" in message


def test_after_activation_a_bare_server_command_resolves_to_its_package_s_script(plugins_dir: Path) -> None:
    activate_plugins_dir()
    # Installed after activation: the provider is asked at every launch.
    bin_dir = _server_build(plugins_dir, "tai42-fixture-srv", "tai42-fixture-srv-mcp")

    assert resolve_stdio_command("tai42-fixture-srv-mcp", {}) == str(bin_dir / "tai42-fixture-srv-mcp")


def test_after_activation_a_bare_python_does_not_resolve_into_a_server_build(
    plugins_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_bin = plugins_dir.parent / "image-bin"
    image_bin.mkdir()
    (image_bin / "python").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(image_bin))
    activate_plugins_dir()
    _server_build(plugins_dir, "tai42-fixture-srv", "tai42-fixture-srv-mcp")

    assert resolve_stdio_command("python", {}) == str(image_bin / "python")
