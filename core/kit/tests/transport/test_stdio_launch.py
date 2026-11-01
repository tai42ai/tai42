"""The stdio launch: a bare stdio command is found on the server env's PATH when it sets
one, then among the commands the declared provider exposes at that moment, then on this
process's PATH when the server env sets none; a file beside an exposed command that is not
itself exposed is never reached through the provider; a path command launches as given; a
bare name with no executable match raises naming where it looked. Every child is launched
with its env exactly as the server declared it."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from fastmcp.client.transports.stdio import StdioTransport
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_kit.transport import (
    declare_stdio_launch,
    declared_stdio_launch,
    get_mcp_transport,
    resolve_stdio_command,
)
from tai42_kit.transport import stdio_launch as stdio_launch_module


@pytest.fixture(autouse=True)
def _undeclared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the undeclared provider and leaves none behind."""
    monkeypatch.setattr(stdio_launch_module, "_LAUNCH", stdio_launch_module._no_exposed_commands)


def _expose(*executables: Path) -> None:
    declare_stdio_launch(lambda: {executable.name: str(executable) for executable in executables})


def _script(directory: Path, name: str, *, executable: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755 if executable else 0o644)
    return script


# -- resolution order -------------------------------------------------------------


def test_an_exposed_command_resolves_before_this_process_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    installed = _script(tmp_path / "installed", "srv")
    _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _expose(installed)

    assert resolve_stdio_command("srv", {}) == str(installed)


def test_a_file_beside_an_exposed_command_is_not_reached_through_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # An environment's interpreter and its dependencies' scripts sit in the same bin as its
    # exposed command; only the exposed command is found through the provider.
    installed = _script(tmp_path / "installed", "srv")
    _script(tmp_path / "installed", "python")
    _script(tmp_path / "installed", "dep-tool")
    image_python = _script(tmp_path / "on-path", "python")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _expose(installed)

    assert resolve_stdio_command("python", {}) == str(image_python)
    with pytest.raises(FileNotFoundError):
        resolve_stdio_command("dep-tool", {})


def test_the_provider_is_asked_at_every_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A command the provider starts exposing after the declaration (a server package
    # installed while the process runs) is found without a new declaration.
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    exposed: dict[str, str] = {}
    calls: list[int] = []

    def provider() -> Mapping[str, str]:
        calls.append(1)
        return dict(exposed)

    declare_stdio_launch(provider)
    with pytest.raises(FileNotFoundError):
        resolve_stdio_command("srv", {})
    installed = _script(tmp_path / "installed", "srv")
    exposed["srv"] = str(installed)

    assert resolve_stdio_command("srv", {}) == str(installed)
    assert len(calls) == 2


def test_a_non_executable_exposed_command_is_passed_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    unusable = _script(tmp_path / "installed", "srv", executable=False)
    on_path = _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _expose(unusable)

    assert resolve_stdio_command("srv", {}) == str(on_path)


def test_a_command_not_exposed_resolves_on_this_process_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    on_path = _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _expose(_script(tmp_path / "installed", "other-srv"))

    assert resolve_stdio_command("srv", {}) == str(on_path)


def test_without_a_declaration_a_bare_command_resolves_on_a_path_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    on_path = _script(tmp_path / "on-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))

    assert dict(declared_stdio_launch()()) == {}
    assert resolve_stdio_command("srv", {}) == str(on_path)


def test_the_server_env_path_is_searched_instead_of_this_process_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _script(tmp_path / "process-path", "srv")
    child_path = _script(tmp_path / "child-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "process-path"))

    assert resolve_stdio_command("srv", {"PATH": str(tmp_path / "child-path")}) == str(child_path)


def test_an_exposed_command_is_found_after_an_entry_path_that_lacks_it(tmp_path: Path):
    installed = _script(tmp_path / "installed", "srv")
    _expose(installed)

    assert resolve_stdio_command("srv", {"PATH": str(tmp_path / "entry")}) == str(installed)


def test_the_entry_path_is_searched_before_the_exposed_commands(tmp_path: Path):
    entry = _script(tmp_path / "entry", "srv")
    _expose(_script(tmp_path / "installed", "srv"))

    assert resolve_stdio_command("srv", {"PATH": str(tmp_path / "entry")}) == str(entry)


def test_this_process_path_is_not_searched_when_the_entry_sets_a_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _script(tmp_path / "process-path", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "process-path"))
    _expose(_script(tmp_path / "installed", "other-srv"))

    with pytest.raises(FileNotFoundError) as caught:
        resolve_stdio_command("srv", {"PATH": str(tmp_path / "entry")})

    message = str(caught.value)
    assert str(tmp_path / "entry") in message
    assert "'other-srv'" in message
    assert str(tmp_path / "process-path") not in message


@pytest.mark.parametrize("command", ["/opt/tools/srv", "./bin/srv", "bin/srv"])
def test_a_command_with_a_directory_part_launches_as_given(tmp_path: Path, command: str):
    _expose(_script(tmp_path / "installed", "srv"))

    assert resolve_stdio_command(command, {}) == command


def test_a_bare_command_found_nowhere_raises_naming_the_path_and_the_exposed_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _expose(_script(tmp_path / "installed", "srv"))

    with pytest.raises(FileNotFoundError) as caught:
        resolve_stdio_command("absent-srv", {})

    message = str(caught.value)
    assert "'absent-srv'" in message
    assert "the exposed commands ['srv']" in message
    assert str(tmp_path / "installed") not in message
    assert str(tmp_path / "on-path") in message


@pytest.mark.parametrize("present", [True, False], ids=["not-executable", "missing"])
def test_an_unusable_exposed_command_is_named_in_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool
):
    unusable = tmp_path / "installed" / "srv"
    if present:
        _script(tmp_path / "installed", "srv", executable=False)
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    _expose(unusable)

    with pytest.raises(PermissionError) as caught:
        resolve_stdio_command("srv", {})

    message = str(caught.value)
    assert str(unusable) in message
    assert "not executable" in message


def test_a_later_declaration_replaces_the_earlier_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    old = _script(tmp_path / "old", "srv")
    new = _script(tmp_path / "new", "srv")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    _expose(old)
    _expose(new)

    assert resolve_stdio_command("srv", {}) == str(new)


# -- the command-server transport -------------------------------------------------


def test_the_command_server_transport_carries_the_resolved_command_and_the_declared_env(tmp_path: Path):
    installed = _script(tmp_path / "installed", "srv")
    _expose(installed)
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(command="srv", args=["--stdio"], env={"K": "v"}))

    transport = get_mcp_transport(cfg)

    assert isinstance(transport, StdioTransport)
    assert (transport.command, transport.args, transport.env) == (str(installed), ["--stdio"], {"K": "v"})


def test_an_exposed_command_gets_no_pythonpath(tmp_path: Path):
    _expose(_script(tmp_path / "installed", "srv"))

    transport = get_mcp_transport(TaiMCPConfig(title="srv", config=MCPConfig(command="srv")))

    assert isinstance(transport, StdioTransport)
    assert transport.env is not None
    assert "PYTHONPATH" not in transport.env


def test_the_command_server_transport_raises_for_a_command_found_nowhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(command="absent-srv"))

    with pytest.raises(FileNotFoundError, match="absent-srv"):
        get_mcp_transport(cfg)
