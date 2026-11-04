"""The ``tai`` entry that runs a process prelude first, and the import-light package root it needs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest
from tai42_contract.backend.runtime import PROCESS_PRELUDE_ENV

from tai42_cli import process_prelude


def _python(code: str, *, cwd: Path, extra_path: Path | None = None, args: list[str] | None = None) -> str:
    env = dict(os.environ)
    env.pop(PROCESS_PRELUDE_ENV, None)
    if extra_path is not None:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(extra_path), env.get("PYTHONPATH")]))
    completed = subprocess.run(
        [sys.executable, *(args or ["-c", code])],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_importing_the_package_root_imports_neither_the_cli_framework_nor_the_contract(tmp_path: Path) -> None:
    out = _python(
        "import sys, tai42_cli, json; "
        "print(json.dumps(sorted(m for m in ('typer', 'click', 'tai42_contract', 'tai42_cli.commands') "
        "if m in sys.modules)))",
        cwd=tmp_path,
    )
    assert json.loads(out) == []


def test_the_package_root_still_serves_app_context() -> None:
    import tai42_cli
    from tai42_cli.commands import _common

    assert tai42_cli.app_context is _common.app_context


def test_the_package_root_refuses_an_unknown_attribute() -> None:
    import tai42_cli

    with pytest.raises(AttributeError, match="no_such_name"):
        _ = tai42_cli.no_such_name


def test_the_entry_runs_the_prelude_before_the_cli_or_the_contract_is_imported(tmp_path: Path) -> None:
    (tmp_path / "recording_prelude.py").write_text(
        textwrap.dedent(
            """
            import json, os, sys

            def record():
                with open(os.environ["PRELUDE_RECORD"], "w") as f:
                    json.dump({
                        "cli_app_imported": "tai42_cli.app" in sys.modules,
                        "contract_imported": "tai42_contract" in sys.modules,
                        "marker": os.environ.get("TAI_PROCESS_PRELUDE"),
                        "argv_tail": sys.argv[1:],
                    }, f)
            """
        )
    )
    record = tmp_path / "record.json"
    os.environ["PRELUDE_RECORD"] = str(record)
    try:
        out = _python(
            "",
            cwd=tmp_path,
            extra_path=tmp_path,
            args=["-P", "-m", "tai42_cli.process_prelude", "recording_prelude:record", "--help"],
        )
    finally:
        del os.environ["PRELUDE_RECORD"]
    assert json.loads(record.read_text()) == {
        "cli_app_imported": False,
        "contract_imported": False,
        "marker": None,
        "argv_tail": ["--help"],
    }
    assert "Usage:" in out


def test_the_entry_records_the_prelude_and_runs_tai_on_the_rest_of_the_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    seen: dict[str, object] = {}

    def fake_prelude() -> None:
        ran.append("prelude")

    def fake_app() -> None:
        seen["argv"] = list(sys.argv)
        seen["marker"] = os.environ.get(PROCESS_PRELUDE_ENV)
        seen["after"] = list(ran)

    monkeypatch.setitem(sys.modules, "fake_prelude_mod", types.SimpleNamespace(go=fake_prelude))
    monkeypatch.setitem(sys.modules, "tai42_cli.app", types.SimpleNamespace(app=fake_app))
    monkeypatch.delenv(PROCESS_PRELUDE_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["entry", "fake_prelude_mod:go", "backend", "worker", "-P", "gevent"])
    with monkeypatch.context() as m:
        m.setattr(os, "environ", dict(os.environ))
        process_prelude._main()
        assert seen == {
            "argv": ["entry", "backend", "worker", "-P", "gevent"],
            "marker": "fake_prelude_mod:go",
            "after": ["prelude"],
        }


def test_the_entry_refuses_a_missing_prelude_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["entry"])
    with pytest.raises(SystemExit, match="module:callable"):
        process_prelude._main()


def test_relaunch_replaces_the_process_with_the_entry_the_same_tai_arguments_and_the_given_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        calls.append((path, list(argv), env))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(sys, "argv", ["/venv/bin/tai", "backend", "worker", "-P", "gevent"])
    monkeypatch.setenv("BUILT_BY_THIS_IMAGE", "1")
    started_with = {"PATH": "/usr/bin", "STARTED_WITH": "yes"}
    with pytest.raises(SystemExit):
        process_prelude.relaunch("celery:maybe_patch_concurrency", started_with)
    assert calls == [
        (
            sys.executable,
            [
                sys.executable,
                "-P",
                "-m",
                "tai42_cli.process_prelude",
                "celery:maybe_patch_concurrency",
                "backend",
                "worker",
                "-P",
                "gevent",
            ],
            {"PATH": "/usr/bin", "STARTED_WITH": "yes"},
        )
    ]
