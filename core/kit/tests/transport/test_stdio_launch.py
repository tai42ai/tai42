"""The stdio launch: a bare stdio command is found on the server env's PATH when it sets
one, then in the declared install locations in their order, then on this process's PATH
when the server env sets none; a path command launches as given; a bare name with no
executable match raises naming where it looked. A child launched from a declared location
gets that location's import directories on its PYTHONPATH, after the server's own entries;
any other child's env is passed as declared."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest
from mcp.client.stdio import get_default_environment
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_kit.transport import (
    ResolvedStdioCommand,
    ScriptLocation,
    StdioLaunch,
    declare_stdio_launch,
    declared_stdio_launch,
    get_mcp_transport,
    resolve_stdio_command,
    stdio_child_env,
)
from tai42_kit.transport import stdio_launch as stdio_launch_module


@pytest.fixture(autouse=True)
def _undeclared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no declaration and leaves none behind."""
    monkeypatch.setattr(stdio_launch_module, "_LAUNCH", StdioLaunch(locations=()))


def _declare(*locations: ScriptLocation) -> None:
    declare_stdio_launch(StdioLaunch(locations))


def _script(directory: Path, name: str, *, executable: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755 if executable else 0o644)
    return script


def _executable(command: str, env: dict[str, str]) -> str:
    return resolve_stdio_command(command, env).executable


# -- resolution order -------------------------------------------------------------


def test_a_bare_command_resolves_in_a_declared_location_before_this_process_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    installed = _script(tmp_path / "installed", "srv")
    _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _declare(ScriptLocation(str(tmp_path / "installed")))

    assert _executable("srv", {}) == str(installed)


def test_the_declared_locations_are_searched_in_their_order(tmp_path: Path):
    first = _script(tmp_path / "first", "srv")
    _script(tmp_path / "second", "srv")
    _declare(
        ScriptLocation(str(tmp_path / "missing")),
        ScriptLocation(str(tmp_path / "first")),
        ScriptLocation(str(tmp_path / "second")),
    )

    assert _executable("srv", {}) == str(first)


def test_a_non_executable_file_in_a_declared_location_is_passed_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _script(tmp_path / "installed", "srv", executable=False)
    on_path = _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _declare(ScriptLocation(str(tmp_path / "installed")))

    assert _executable("srv", {}) == str(on_path)


def test_a_command_in_no_declared_location_resolves_on_this_process_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    on_path = _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _declare(ScriptLocation(str(tmp_path / "installed")))

    assert _executable("srv", {}) == str(on_path)


def test_without_a_declaration_a_bare_command_resolves_on_a_path_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    on_path = _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))

    assert declared_stdio_launch() == StdioLaunch(locations=())
    assert _executable("srv", {}) == str(on_path)


def test_the_server_env_path_is_searched_instead_of_this_process_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _script(tmp_path / "process-path", "srv")
    child_path = _script(tmp_path / "child-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "process-path"))

    assert _executable("srv", {"PATH": str(tmp_path / "child-path")}) == str(child_path)


def test_a_declared_location_is_searched_after_an_entry_path_that_lacks_the_command(tmp_path: Path):
    installed = _script(tmp_path / "installed", "srv")
    _declare(ScriptLocation(str(tmp_path / "installed")))

    assert _executable("srv", {"PATH": str(tmp_path / "entry")}) == str(installed)


def test_the_entry_path_is_searched_before_the_declared_locations(tmp_path: Path):
    entry = _script(tmp_path / "entry", "srv")
    _script(tmp_path / "installed", "srv")
    _declare(ScriptLocation(str(tmp_path / "installed")))

    assert _executable("srv", {"PATH": str(tmp_path / "entry")}) == str(entry)


def test_this_process_path_is_not_searched_when_the_entry_sets_a_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _script(tmp_path / "process-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "process-path"))
    _declare(ScriptLocation(str(tmp_path / "installed")))

    with pytest.raises(FileNotFoundError) as caught:
        resolve_stdio_command("srv", {"PATH": str(tmp_path / "entry")})

    message = str(caught.value)
    assert str(tmp_path / "entry") in message
    assert str(tmp_path / "installed") in message
    assert str(tmp_path / "process-path") not in message


@pytest.mark.parametrize("command", ["/opt/tools/srv", "./bin/srv", "bin/srv"])
def test_a_command_with_a_directory_part_launches_as_given(command: str):
    _declare(ScriptLocation("/opt/tools", ("/pfx/site",)))

    assert resolve_stdio_command(command, {}) == ResolvedStdioCommand(command, ())


def test_a_bare_command_found_nowhere_raises_naming_where_it_looked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _declare(ScriptLocation(str(tmp_path / "installed")))

    with pytest.raises(FileNotFoundError) as caught:
        resolve_stdio_command("absent-srv", {})

    message = str(caught.value)
    assert "'absent-srv'" in message
    assert str(tmp_path / "installed") in message
    assert str(tmp_path / "on-path") in message


def test_a_non_executable_match_is_named_in_the_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    unusable = _script(tmp_path / "installed", "srv", executable=False)
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _declare(ScriptLocation(str(tmp_path / "installed")))

    with pytest.raises(PermissionError) as caught:
        resolve_stdio_command("srv", {})

    message = str(caught.value)
    assert str(unusable) in message
    assert "not executable" in message


def test_a_later_declaration_replaces_the_earlier_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _script(tmp_path / "old", "srv")
    new = _script(tmp_path / "new", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    _declare(ScriptLocation(str(tmp_path / "old")))
    _declare(ScriptLocation(str(tmp_path / "new")))

    assert _executable("srv", {}) == str(new)


# -- import dirs by where the command is found ------------------------------------


def test_a_command_found_in_a_location_carries_its_import_dirs(tmp_path: Path):
    installed = _script(tmp_path / "pfx-bin", "srv")
    _declare(ScriptLocation(str(tmp_path / "env-bin")), ScriptLocation(str(tmp_path / "pfx-bin"), ("/pfx/a", "/pfx/b")))

    assert resolve_stdio_command("srv", {}) == ResolvedStdioCommand(str(installed), ("/pfx/a", "/pfx/b"))


def test_a_command_found_on_a_path_gets_no_import_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    on_path = _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _declare(ScriptLocation(str(tmp_path / "pfx-bin"), ("/pfx/site",)))

    assert resolve_stdio_command("srv", {}) == ResolvedStdioCommand(str(on_path), ())
    transport = get_mcp_transport(TaiMCPConfig(title="srv", config=MCPConfig(command="srv")))
    assert isinstance(transport, dict)
    assert "PYTHONPATH" not in transport["mcpServers"]["srv"]["env"]


def test_a_command_found_in_a_location_without_import_dirs_gets_none(tmp_path: Path):
    environment = _script(tmp_path / "env-bin", "srv")
    _declare(ScriptLocation(str(tmp_path / "env-bin")), ScriptLocation(str(tmp_path / "pfx-bin"), ("/pfx/site",)))
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(command="srv", env={"K": "v"}))

    transport = get_mcp_transport(cfg)

    assert isinstance(transport, dict)
    assert transport["mcpServers"]["srv"]["command"] == str(environment)
    assert transport["mcpServers"]["srv"]["env"] == {"K": "v"}


def test_a_command_found_on_the_entry_path_runs_without_the_declared_import_dirs(tmp_path: Path):
    # A server of another environment holds its own copy of a package the declared
    # location's import dirs also hold; found on the entry's PATH, it imports its own.
    prefix_site = tmp_path / "pfx" / "site"
    (prefix_site / "shared_probe_pkg").mkdir(parents=True)
    (prefix_site / "shared_probe_pkg" / "__init__.py").write_text("", encoding="utf-8")
    other_lib = tmp_path / "other" / "lib"
    (other_lib / "shared_probe_pkg").mkdir(parents=True)
    (other_lib / "shared_probe_pkg" / "__init__.py").write_text("", encoding="utf-8")
    other_bin = tmp_path / "other" / "bin"
    other_bin.mkdir(parents=True)
    script = other_bin / "other-srv"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.append({str(other_lib)!r})\n"
        "import shared_probe_pkg\nprint(shared_probe_pkg.__file__)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    _declare(ScriptLocation(str(tmp_path / "pfx" / "bin"), (str(prefix_site),)))
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(command="other-srv", env={"PATH": str(other_bin)}))

    transport = get_mcp_transport(cfg)
    assert isinstance(transport, dict)
    server = transport["mcpServers"]["srv"]
    launched = subprocess.run(
        [server["command"]],
        env={**get_default_environment(), **server["env"]},
        capture_output=True,
        text=True,
        check=False,
    )

    assert server["env"] == {"PATH": str(other_bin)}
    assert launched.returncode == 0, launched.stderr
    assert str(other_lib) in launched.stdout


# -- child env --------------------------------------------------------------------


def test_without_import_dirs_the_child_env_is_the_server_env():
    assert stdio_child_env({"K": "v"}, ()) == {"K": "v"}


def test_import_dirs_become_the_child_pythonpath_in_their_order():
    assert stdio_child_env({}, ["/pfx/purelib", "/pfx/platlib"])["PYTHONPATH"] == os.pathsep.join(
        ["/pfx/purelib", "/pfx/platlib"]
    )


def test_the_server_own_pythonpath_leads_the_import_dirs():
    own = os.pathsep.join(["/own/a", "", "/own/b"])

    assert stdio_child_env({"PYTHONPATH": own}, ["/pfx/site"])["PYTHONPATH"] == os.pathsep.join(
        ["/own/a", "/own/b", "/pfx/site"]
    )


def test_other_child_env_keys_are_untouched():
    env = {"PATH": "/srv/bin", "PLUGIN_KEY": "value"}

    child = stdio_child_env(env, ["/pfx/site"])

    assert child == {**env, "PYTHONPATH": "/pfx/site"}
    assert env == {"PATH": "/srv/bin", "PLUGIN_KEY": "value"}


# -- the command-server transport -------------------------------------------------


def test_the_command_server_transport_carries_the_resolved_command_and_child_env(tmp_path: Path):
    installed = _script(tmp_path / "installed", "srv")
    _declare(ScriptLocation(str(tmp_path / "installed"), ("/pfx/site",)))
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(command="srv", args=["--stdio"], env={"K": "v"}))

    transport = get_mcp_transport(cfg)

    assert transport == {
        "mcpServers": {
            "srv": {
                **cfg.config.model_dump(exclude_none=True),
                "command": str(installed),
                "env": {"K": "v", "PYTHONPATH": "/pfx/site"},
            }
        }
    }


def test_the_command_server_transport_raises_for_a_command_found_nowhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(command="absent-srv"))

    with pytest.raises(FileNotFoundError, match="absent-srv"):
        get_mcp_transport(cfg)


def test_a_console_script_installed_under_a_declared_prefix_runs_with_its_imports(tmp_path: Path):
    # The layout ``pip install --prefix`` writes: the package under the prefix's site dir,
    # and a console script in the prefix's ``bin`` whose shebang is the launching
    # interpreter, which does not see the prefix on its own.
    prefix = tmp_path / "pfx"
    paths = sysconfig.get_paths(vars={"base": str(prefix), "platbase": str(prefix)})
    package = Path(paths["purelib"]) / "launch_probe_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("print(__file__)\n", encoding="utf-8")
    bin_dir = Path(paths["scripts"])
    bin_dir.mkdir(parents=True)
    script = bin_dir / "launch-probe"
    script.write_text(f"#!{sys.executable}\nimport launch_probe_pkg\n", encoding="utf-8")
    script.chmod(0o755)
    _declare(ScriptLocation(str(bin_dir), (paths["purelib"],)))

    transport = get_mcp_transport(TaiMCPConfig(title="probe", config=MCPConfig(command="launch-probe")))
    assert isinstance(transport, dict)
    server = transport["mcpServers"]["probe"]
    launched = subprocess.run(
        [server["command"]],
        env={**get_default_environment(), **server["env"]},
        capture_output=True,
        text=True,
        check=False,
    )

    assert launched.returncode == 0, launched.stderr
    assert str(package) in launched.stdout
