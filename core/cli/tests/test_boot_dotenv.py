"""The ``tai`` root callback's ``.env`` load never loads the stored env's own file.

In file mode the stored env is ``<TAI_CONFIG_DIR_PATH>/.env``. A server started through
``tai serve`` / ``tai backend`` / ``tai metrics`` runs this callback before its first stored-env
apply, so a stored key loaded here would look set by the deployment. ``python-dotenv`` searches
from the working directory when a trace function is set; with the working directory at the
config dir that search finds the stored env's file, and the load skips it with a warning. A
``.env`` that names its own folder as the config dir is refused. Any other ``.env`` it finds
still loads.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result
from tai42_contract.config.manager import file_config_dir

from tai42_cli import app as app_module

_STORED_KEY = "TAI_BOOT_DOTENV_PROBE_STORED"
_LOCAL_KEY = "TAI_BOOT_DOTENV_PROBE_LOCAL"


@pytest.fixture
def _traced() -> Iterator[None]:
    """Run the test with a trace function set, as under a debugger."""
    previous = sys.gettrace()
    sys.settrace(lambda frame, event, arg: None)
    try:
        yield
    finally:
        sys.settrace(previous)


def _root_callback_result(monkeypatch: pytest.MonkeyPatch) -> Result:
    monkeypatch.setattr(app_module, "entry_points", lambda group: [])
    group = app_module._build_app()

    @click.command("probe")
    def probe() -> None:
        pass

    group.add_command(probe, "probe")
    return CliRunner().invoke(group, ["probe"])


def _invoke_root_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _root_callback_result(monkeypatch)
    assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("_traced")
def test_the_stored_env_file_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"{_STORED_KEY}=a\n")
    monkeypatch.chdir(config_dir)
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(config_dir))
    monkeypatch.delenv("TAI_CONFIG_MODE", raising=False)
    monkeypatch.delenv(_STORED_KEY, raising=False)

    with caplog.at_level(logging.WARNING):
        _invoke_root_callback(monkeypatch)

    assert _STORED_KEY not in os.environ
    assert any(str(config_dir / ".env") in record.getMessage() for record in caplog.records)


@pytest.mark.usefixtures("_traced")
def test_a_local_env_file_naming_its_own_dir_as_the_config_dir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"TAI_CONFIG_DIR_PATH=.\n{_STORED_KEY}=a\n")
    monkeypatch.chdir(config_dir)
    monkeypatch.delenv("TAI_CONFIG_DIR_PATH", raising=False)
    monkeypatch.delenv("TAI_CONFIG_MODE", raising=False)
    monkeypatch.delenv(_STORED_KEY, raising=False)

    result = _root_callback_result(monkeypatch)

    assert isinstance(result.exception, RuntimeError), result.output
    message = str(result.exception)
    assert str(config_dir / ".env") in message
    assert "TAI_CONFIG_DIR_PATH" in message
    assert "TAI_CONFIG_DIR_PATH" not in os.environ
    assert _STORED_KEY not in os.environ


@pytest.mark.usefixtures("_traced")
def test_a_launch_env_file_naming_another_config_dir_loads_fully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "work"
    (work_dir / "app").mkdir(parents=True)
    (work_dir / "app" / ".env").write_text(f"{_STORED_KEY}=a\n")
    (work_dir / ".env").write_text(f"TAI_CONFIG_DIR_PATH=./app\n{_LOCAL_KEY}=b\n")
    monkeypatch.chdir(work_dir)
    monkeypatch.delenv("TAI_CONFIG_DIR_PATH", raising=False)
    monkeypatch.delenv("TAI_CONFIG_MODE", raising=False)
    monkeypatch.delenv(_LOCAL_KEY, raising=False)
    monkeypatch.delenv(_STORED_KEY, raising=False)

    _invoke_root_callback(monkeypatch)

    assert os.environ.get("TAI_CONFIG_DIR_PATH") == "./app"
    assert os.environ.get(_LOCAL_KEY) == "b"
    assert file_config_dir(os.environ) == "./app"
    assert _STORED_KEY not in os.environ


@pytest.mark.usefixtures("_traced")
def test_another_env_file_still_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"{_STORED_KEY}=a\n")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / ".env").write_text(f"{_LOCAL_KEY}=b\n")
    monkeypatch.chdir(work_dir)
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(config_dir))
    monkeypatch.delenv("TAI_CONFIG_MODE", raising=False)
    monkeypatch.delenv(_LOCAL_KEY, raising=False)

    _invoke_root_callback(monkeypatch)

    assert os.environ.get(_LOCAL_KEY) == "b"
    assert _STORED_KEY not in os.environ
