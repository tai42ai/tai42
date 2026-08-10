"""Slim client install — ``tai42-cli`` + ``tai42-contract`` ONLY, no server.

The remote half of the ``tai`` command must operate a booted stack while installed
on its own. This builds the ``tai42-contract`` and ``tai42-cli`` wheels, installs
ONLY those into a fresh venv (the server package is absent), and drives the slim
``tai`` against the B7 stack: a remote read answers, ``auth whoami`` answers,
``version --json`` lists ``tai42-cli`` and NOT ``tai42-skeleton``, a pure-remote
``config`` command works, and the server-contributed commands (``serve``,
``config lint``) are simply absent.

The full-tree B7 file (``test_cli_against_stack``) still covers the workspace ``tai``
with the server installed; this leg is the standalone-install contract.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tai42_e2e.stack import TaiStack

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def slim_tai(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the client + contract wheels, install ONLY them into a fresh venv, and
    return the slim ``tai`` console script (the server package is never installed)."""
    wheels_dir = tmp_path_factory.mktemp("slim-wheels")
    for package in ("tai42-contract", "tai42-cli"):
        built = subprocess.run(
            ["uv", "build", "--package", package, "--wheel", "--out-dir", str(wheels_dir)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert built.returncode == 0, f"uv build {package} failed:\n{built.stderr}"

    venv_dir = tmp_path_factory.mktemp("slim-venv")
    made = subprocess.run(
        ["uv", "venv", "--python", "3.13", str(venv_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert made.returncode == 0, f"uv venv failed:\n{made.stderr}"

    venv_python = venv_dir / "bin" / "python"
    wheels = [str(path) for path in sorted(wheels_dir.glob("*.whl"))]
    # Install the two local wheels by path; their third-party deps resolve normally,
    # but tai42-skeleton is not among them, so it is never pulled in.
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), *wheels],
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, f"slim install failed:\n{installed.stderr}"

    tai = venv_dir / "bin" / "tai"
    assert tai.exists(), "the slim install did not provide the 'tai' console script"
    return tai


def _run(tai: Path, stack: TaiStack, *args: str, json_flag: bool = True) -> subprocess.CompletedProcess[str]:
    """Run the slim ``tai --server <url> [--json] <args...>`` against the stack, auth
    over ``TAI_API_KEY``. A clean child env keeps only the slim venv on PATH."""
    assert stack.auth_token is not None, "the CLI stack must be seeded with a root token"
    env = {
        "PATH": os.pathsep.join([str(tai.parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", str(tai.parent)),
        "TAI_API_KEY": stack.auth_token,
    }
    server = f"http://{stack.host}:{stack.port_a}"
    argv = [str(tai), "--server", server, *(["--json"] if json_flag else []), *args]
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90)


def test_slim_client_reads_remote(slim_tai: Path, cli_stack: TaiStack) -> None:
    tools = _run(slim_tai, cli_stack, "tools", "list", json_flag=False)
    assert tools.returncode == 0, tools.stderr
    assert "generate_uuid" in tools.stdout, tools.stdout


def test_slim_client_auth_whoami_answers(slim_tai: Path, cli_stack: TaiStack) -> None:
    whoami = _run(slim_tai, cli_stack, "auth", "whoami")
    assert whoami.returncode == 0, whoami.stderr
    assert whoami.stdout.strip(), whoami.stdout


def test_slim_client_pure_remote_config_works(slim_tai: Path, cli_stack: TaiStack) -> None:
    # A pure-remote config command works against the stack (contrast with the absent
    # offline ``config lint`` below).
    env_get = _run(slim_tai, cli_stack, "config", "env", "get")
    assert env_get.returncode == 0, env_get.stderr


def test_slim_version_lists_cli_not_skeleton(slim_tai: Path, cli_stack: TaiStack) -> None:
    result = _run(slim_tai, cli_stack, "version")
    assert result.returncode == 0, result.stderr
    packages = {row["package"] for row in json.loads(result.stdout)}
    assert "tai42-cli" in packages
    assert "tai42-skeleton" not in packages


def test_slim_client_lacks_server_command(slim_tai: Path, cli_stack: TaiStack) -> None:
    serve = _run(slim_tai, cli_stack, "serve", "--help")
    assert serve.returncode != 0
    assert "No such command" in serve.stderr


def test_slim_client_lacks_offline_config_lint(slim_tai: Path, cli_stack: TaiStack) -> None:
    lint = _run(slim_tai, cli_stack, "config", "lint", "--help")
    assert lint.returncode != 0
    assert "No such command" in lint.stderr
