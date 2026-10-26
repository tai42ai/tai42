"""The boot ``.env`` load of every server entry point leaves the stored env out of the deployment env.

Each entry point (``tai serve``'s process ``main``, ``tai backend``, ``tai metrics``) loads a
local ``.env`` into ``os.environ`` in file mode, before the first stored-env apply captures the
deployment env. ``python-dotenv`` searches from the working directory when a trace function is
set, so with the working directory at the config dir that search finds the stored env's own
file. Loading it would make every stored key look set by the deployment. The load skips that
file and says so; a ``.env`` that names its own folder as the config dir refuses the boot; any
other ``.env`` it finds still loads.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

import tai42_skeleton.cli.backend as backend
import tai42_skeleton.cli.mcp_app as mcp_app
import tai42_skeleton.cli.metrics as metrics
from tai42_skeleton.config.deployment_env import capture_deployment_env, deployment_env_keys, reset_deployment_env

_STORED_KEY = "TAI_BOOT_DOTENV_PROBE_STORED"
_LOCAL_KEY = "TAI_BOOT_DOTENV_PROBE_LOCAL"


async def _noop() -> None:
    return None


def _run_serve_main(monkeypatch: pytest.MonkeyPatch) -> BaseException | None:
    monkeypatch.setattr(mcp_app, "cli", lambda: None)
    monkeypatch.setattr(mcp_app, "config_mode", lambda: "file")
    try:
        mcp_app.main()
    except Exception as exc:
        return exc
    return None


def _run_backend_main(monkeypatch: pytest.MonkeyPatch) -> BaseException | None:
    import uvloop

    monkeypatch.setattr(backend, "config_mode", lambda: "file")
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop())
    monkeypatch.setattr(uvloop, "run", lambda coro: coro.close())
    return CliRunner().invoke(backend.main, ["worker"]).exception


def _run_metrics_main(monkeypatch: pytest.MonkeyPatch) -> BaseException | None:
    monkeypatch.setattr(metrics, "config_mode", lambda: "file")
    monkeypatch.setattr(metrics, "create_app", lambda: object())
    monkeypatch.setattr(metrics.uvicorn, "run", lambda app, **kw: None)
    return CliRunner().invoke(metrics.main, []).exception


_ENTRY_POINTS: dict[str, Callable[[pytest.MonkeyPatch], BaseException | None]] = {
    "serve": _run_serve_main,
    "backend": _run_backend_main,
    "metrics": _run_metrics_main,
}


@pytest.fixture(autouse=True)
def _restored_environ() -> Iterator[None]:
    """The dotenv load writes ``os.environ`` directly; put it back after each test."""
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def _traced() -> Iterator[None]:
    """Run the test with a trace function set, as under a debugger."""
    previous = sys.gettrace()
    sys.settrace(lambda frame, event, arg: None)
    try:
        yield
    finally:
        sys.settrace(previous)


@pytest.mark.usefixtures("_traced")
@pytest.mark.parametrize("entry_point", sorted(_ENTRY_POINTS))
def test_the_stored_env_file_is_not_loaded_at_boot(
    entry_point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"{_STORED_KEY}=a\n")
    monkeypatch.chdir(config_dir)
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(config_dir))
    monkeypatch.delenv(_STORED_KEY, raising=False)

    with caplog.at_level(logging.WARNING):
        assert _ENTRY_POINTS[entry_point](monkeypatch) is None

    reset_deployment_env()
    capture_deployment_env()
    assert _STORED_KEY not in os.environ
    assert _STORED_KEY not in deployment_env_keys()
    assert any(str(config_dir / ".env") in record.getMessage() for record in caplog.records)


@pytest.mark.usefixtures("_traced")
@pytest.mark.parametrize("entry_point", sorted(_ENTRY_POINTS))
def test_another_env_file_still_loads_at_boot(
    entry_point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"{_STORED_KEY}=a\n")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / ".env").write_text(f"{_LOCAL_KEY}=b\n")
    monkeypatch.chdir(work_dir)
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(config_dir))
    monkeypatch.delenv(_LOCAL_KEY, raising=False)

    assert _ENTRY_POINTS[entry_point](monkeypatch) is None

    reset_deployment_env()
    capture_deployment_env()
    assert os.environ.get(_LOCAL_KEY) == "b"
    assert _LOCAL_KEY in deployment_env_keys()


@pytest.mark.usefixtures("_traced")
@pytest.mark.parametrize("entry_point", sorted(_ENTRY_POINTS))
def test_a_local_env_file_naming_its_own_dir_as_the_config_dir_refuses_the_boot(
    entry_point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"TAI_CONFIG_DIR_PATH=.\n{_STORED_KEY}=a\n")
    monkeypatch.chdir(config_dir)
    monkeypatch.delenv("TAI_CONFIG_DIR_PATH", raising=False)
    monkeypatch.delenv(_STORED_KEY, raising=False)

    refusal = _ENTRY_POINTS[entry_point](monkeypatch)

    assert isinstance(refusal, RuntimeError)
    assert str(config_dir / ".env") in str(refusal)
    assert "TAI_CONFIG_DIR_PATH" in str(refusal)
    assert "TAI_CONFIG_DIR_PATH" not in os.environ
    assert _STORED_KEY not in os.environ
