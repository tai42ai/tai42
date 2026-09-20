"""Metrics infrastructure coverage: the non-wiping ensure-exists initializer, the
master-only wipe (first wipe + sentinel early-return + re-wipe on a new run id),
the writer-only mmap-value-class assert, the absolute/relative multiproc-dir
settings validation, a real cross-process scrape of a spawned worker's counter,
and the mode-aware render fork (multiproc vs in-process, logged once on the first
scrape). Filesystem state is under ``tmp_path``; the cross-process scrape and the
writer import-order probes each spawn a subprocess."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import textwrap

import pytest
from pydantic import ValidationError

from tai42_skeleton.routers import prometheus as prom_mod
from tai42_skeleton.routers.metrics_settings import MetricsSettings
from tai42_skeleton.routers.prometheus import render_multiproc_metrics


def _fake_settings(multiproc_dir: str) -> MetricsSettings:
    return MetricsSettings(prometheus_multiproc_dir=multiproc_dir)


def test_metrics_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite sets ``PROMETHEUS_MULTIPROC_DIR`` (the mmap freeze); drop it so the
    # field resolves to its coded default. The default is a FIXED absolute path
    # (host tempdir), CWD-independent so every run-family process agrees on one dir.
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    settings = MetricsSettings()
    assert settings.backend_metrics_host == "127.0.0.1"
    assert settings.backend_metrics_port == 8012
    expected = os.path.join(tempfile.gettempdir(), "tai42_prometheus")
    assert settings.prometheus_multiproc_dir == expected
    assert os.path.isabs(settings.prometheus_multiproc_dir)  # the default passes its own validator


def test_metrics_settings_rejects_relative_override_argument() -> None:
    # A relative override resolves per-CWD and splits the shared dir — refuse loudly.
    with pytest.raises(ValidationError, match="absolute path"):
        MetricsSettings(prometheus_multiproc_dir="relative/tai42_prometheus")


def test_metrics_settings_rejects_relative_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "relative/tai42_prometheus")
    with pytest.raises(ValidationError, match="absolute path"):
        MetricsSettings()


def test_metrics_settings_accepts_absolute_override(tmp_path) -> None:
    settings = MetricsSettings(prometheus_multiproc_dir=str(tmp_path))
    assert settings.prometheus_multiproc_dir == str(tmp_path)


def test_init_ensures_dir_without_wiping(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``init`` is the reader/worker ensure-exists path: it creates the dir if
    missing but NEVER removes a populated one, so a live worker's mmap files
    survive a metrics-server or backend-worker start against a shared dir."""
    target = tmp_path / "metrics"
    target.mkdir()
    (target / "live.db").write_text("live", encoding="utf-8")
    monkeypatch.setattr(prom_mod, "metrics_settings", lambda: _fake_settings(str(target)))

    returned = prom_mod.init_prometheus_multiproc_dir()

    assert returned == str(target)
    assert target.is_dir()
    assert (target / "live.db").exists()  # ensure-only: a pre-existing file survives


def test_init_creates_missing_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "metrics"
    monkeypatch.setattr(prom_mod, "metrics_settings", lambda: _fake_settings(str(target)))

    assert prom_mod.init_prometheus_multiproc_dir() == str(target)
    assert target.is_dir()


def test_wipe_removes_stale_and_stamps_sentinel(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "metrics"
    # Pre-create with a stale file to prove the master wipe clears it.
    target.mkdir()
    (target / "stale.db").write_text("old", encoding="utf-8")
    monkeypatch.setattr(prom_mod, "metrics_settings", lambda: _fake_settings(str(target)))

    returned = prom_mod.wipe_prometheus_multiproc_dir()

    assert returned == str(target)
    assert target.is_dir()
    assert not (target / "stale.db").exists()  # wiped
    assert (target / ".init_done").exists()  # sentinel created


def test_wipe_second_call_same_run_early_returns(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "metrics"
    monkeypatch.setattr(prom_mod, "metrics_settings", lambda: _fake_settings(str(target)))

    prom_mod.wipe_prometheus_multiproc_dir()  # creates sentinel
    marker = target / "keep.db"
    marker.write_text("kept", encoding="utf-8")

    prom_mod.wipe_prometheus_multiproc_dir()  # sentinel present, same run -> no wipe

    assert marker.exists()  # second call did not wipe


def test_wipe_rewipes_on_new_run_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A new run id re-wipes even though the sentinel exists. A pid-based marker
    would wrongly skip the wipe when consecutive runs share a parent pid."""
    target = tmp_path / "metrics"
    monkeypatch.setattr(prom_mod, "metrics_settings", lambda: _fake_settings(str(target)))

    monkeypatch.setenv("TAI_METRICS_RUN_ID", "run-1")
    prom_mod.wipe_prometheus_multiproc_dir()
    kept = target / "keep.db"
    kept.write_text("x", encoding="utf-8")

    monkeypatch.setenv("TAI_METRICS_RUN_ID", "run-2")
    prom_mod.wipe_prometheus_multiproc_dir()

    assert not kept.exists()  # a new run id forced the wipe to re-run


def test_init_does_not_die_when_master_wipe_races_its_makedirs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A worker's ``init`` must survive the master's concurrent wipe of the SAME dir.

    ``os.makedirs(exist_ok=True)`` is not atomic against a remover: its internal
    ``mkdir`` raises ``FileExistsError`` when the dir is present, and the ``exist_ok``
    shortcut only swallows that if the dir still ``isdir`` at its recheck. If the
    master's ``rmtree`` deletes the dir in the window between that failed ``mkdir``
    and the recheck, ``makedirs`` re-raises and the worker dies at boot.

    This forces that exact interleave deterministically (no timing roulette):
    ``os.path.isdir`` (the recheck ``makedirs`` performs on the target after its
    ``mkdir`` fails) is hooked so, on the first recheck for the target, ``init`` parks
    in the window and lets a real ``wipe`` thread run; ``shutil.rmtree`` is hooked to
    park the wipe *after* its delete, before it recreates the dir. On the UNFIXED code
    ``init`` holds no lock, so the wipe's rmtree runs and the recheck finds the dir
    gone -> ``makedirs`` re-raises. With ``init`` and ``wipe`` sharing one lock, the
    wipe blocks on ``init``'s lock, the recheck (bounded wait) finds the dir intact,
    and ``init`` returns cleanly."""
    import shutil
    import threading

    target = tmp_path / "metrics"
    target.mkdir()  # pre-exists so init's internal mkdir fails into the exist_ok recheck
    monkeypatch.setattr(prom_mod, "metrics_settings", lambda: _fake_settings(str(target)))

    init_at_recheck = threading.Event()  # init has entered makedirs' exist_ok recheck
    rmtree_done = threading.Event()  # the racing wipe has deleted the dir
    init_left_window = threading.Event()  # init finished its recheck; wipe may recreate

    real_isdir = os.path.isdir
    real_rmtree = shutil.rmtree
    target_abs = os.path.abspath(str(target))

    def racing_isdir(path, *args, **kwargs):
        # makedirs calls isdir(target) only in its except branch, i.e. exactly the
        # post-mkdir recheck window. On the first such recheck, park here and let the
        # concurrent wipe delete the dir; on fixed code init holds the lock so the
        # wipe cannot run and this bounded wait simply times out.
        if os.path.abspath(str(path)) == target_abs and not init_at_recheck.is_set():
            init_at_recheck.set()
            rmtree_done.wait(timeout=2.0)
        return real_isdir(path, *args, **kwargs)

    def racing_rmtree(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        rmtree_done.set()
        # Hold the wipe after its delete, before it recreates the dir, so init's
        # recheck lands in the delete-then-recreate window (the production race).
        init_left_window.wait(timeout=2.0)

    def wipe_worker() -> None:
        init_at_recheck.wait(timeout=2.0)  # only race once init is parked in its recheck
        prom_mod.wipe_prometheus_multiproc_dir()

    wiper = threading.Thread(target=wipe_worker)
    wiper.start()
    monkeypatch.setattr(os.path, "isdir", racing_isdir)
    monkeypatch.setattr(shutil, "rmtree", racing_rmtree)
    try:
        # Must NOT raise: on the fixed code init holds the shared lock across its
        # makedirs, so the master's rmtree cannot slip into the recheck window.
        prom_mod.init_prometheus_multiproc_dir()
    finally:
        init_left_window.set()  # release the parked wipe so it can finish + the thread joins
        rmtree_done.set()
        wiper.join(timeout=5.0)

    assert target.is_dir()


def test_assert_multiproc_value_class_passes_under_mmap() -> None:
    # The suite freezes the mmap value backend (conftest sets the env before the
    # first ``prometheus_client`` import), so a writer's assert is satisfied.
    prom_mod.assert_multiproc_value_class()  # no raise


def test_assert_multiproc_value_class_raises_under_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MutexValue:
        _multiprocess = False

    monkeypatch.setattr(prom_mod.values, "ValueClass", _MutexValue)
    with pytest.raises(RuntimeError, match="multiprocess mmap backend"):
        prom_mod.assert_multiproc_value_class()


@pytest.mark.parametrize("module", ["tai42_skeleton.cli.mcp_app", "tai42_skeleton.cli.backend"])
def test_writer_cli_import_does_not_preload_prometheus(module: str) -> None:
    """Each WRITER entry point must publish ``PROMETHEUS_MULTIPROC_DIR`` before
    anything imports ``prometheus_client`` (which freezes its value backend). That
    the module import itself pulls in NO ``prometheus_client`` proves the env-set
    inside ``run_mcp_app`` / ``main`` runs first — the import happens only later,
    from inside those functions, after the env is set."""
    probe = (
        f"import sys; import {module}; "
        "assert 'prometheus_client' not in sys.modules, "
        "sorted(m for m in sys.modules if 'prometheus' in m)"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_counter_from_spawned_process_is_visible_to_scrape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A counter incremented in a SEPARATE process whose env sets the shared
    multiproc dir before ``prometheus_client`` imports is visible to a fresh
    collector scrape in this process — the mmap files carry across the process
    boundary."""
    mp_dir = tmp_path / "mp"
    mp_dir.mkdir()

    child = textwrap.dedent(
        """
        from prometheus_client import Counter
        from prometheus_client.values import ValueClass

        # The env (set by the parent below) must have frozen the mmap backend.
        assert ValueClass.__name__ == "MmapedValue", ValueClass.__name__
        counter = Counter("tai_worker_probe_total", "cross-process probe", ["runtime"])
        counter.labels(runtime="worker").inc(3)
        """
    )
    # The env is set BEFORE the child interpreter starts, exactly as a spawned
    # uvicorn worker inherits it from the master — the ordering the mmap backend
    # requires.
    result = subprocess.run(
        [sys.executable, "-c", child],
        env={**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(mp_dir)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    # Scrape from THIS process against the same dir; the collector reads the child's
    # mmap file left behind after it exited.
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(mp_dir))
    exposition = render_multiproc_metrics().decode()

    assert "tai_worker_probe_total" in exposition
    assert 'runtime="worker"' in exposition
    assert 'tai_worker_probe_total{runtime="worker"} 3.0' in exposition


def test_backend_main_activates_multiproc_before_prometheus_import_in_clean_env() -> None:
    """``cli/backend.py::main`` must call ``activate_multiproc_env()`` BEFORE the
    statement that imports ``routers.prometheus`` (which pulls in
    ``prometheus_client`` and freezes its value backend). That is a statement-order
    contract inside one function, invisible to a same-process test because conftest
    already froze the value class for this interpreter.

    Drive ``main`` in a FRESH interpreter with ``PROMETHEUS_MULTIPROC_DIR`` UNSET:
    if activation runs first, the child freezes the mmap value class and the
    in-``main`` ``assert_multiproc_value_class`` passes (exit 0); if the prometheus
    import runs first, it freezes the in-process mutex and that assert raises
    (non-zero exit). ``run_backend`` and the event loop are stubbed so ``main`` runs
    its activate→import→assert prelude without building or serving the app. This is
    the test that catches a writer whose import order would silently lose every tool
    counter to the mutex backend."""
    child = textwrap.dedent(
        """
        import asyncio

        import tai42_skeleton.cli.backend as backend
        from click.testing import CliRunner

        async def _noop():
            return None

        # Stop main before it builds or serves; only its activate+import+assert
        # prelude must run.
        backend.run_backend = lambda args: _noop()
        try:
            import uvloop

            uvloop.run = lambda coro: coro.close()
        except ImportError:
            pass
        asyncio.run = lambda coro: coro.close()

        # catch_exceptions=False so a raised assert propagates to a non-zero exit
        # rather than being swallowed into result.exception.
        result = CliRunner().invoke(backend.main, ["worker"], catch_exceptions=False)
        assert result.exit_code == 0, result.output

        from prometheus_client.values import ValueClass

        assert ValueClass.__name__ == "MmapedValue", ValueClass.__name__
        print("MmapedValue")
        """
    )
    env = os.environ.copy()
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)  # a clean env, as production launches into
    result = subprocess.run([sys.executable, "-c", child], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "MmapedValue" in result.stdout


def test_backend_main_creates_multiproc_dir(tmp_path) -> None:
    """``cli/backend.py::main`` must ensure the shared multiproc dir EXISTS before this
    writer opens its per-process counter files in it. The backend worker is a top-level
    process that cannot rely on the mcp_app master having created the dir, so ``main``
    calls ``init_prometheus_multiproc_dir()`` itself. Drive ``main`` in a FRESH
    interpreter with the env var unset and ``TMPDIR`` redirected into ``tmp_path`` (so the
    coded-default dir lands under tmp): after the activate→init prelude the dir must
    exist. ``run_backend`` and the loop are stubbed so only the prelude runs."""
    child = textwrap.dedent(
        """
        import asyncio
        import os

        import tai42_skeleton.cli.backend as backend
        from click.testing import CliRunner

        async def _noop():
            return None

        # Stop main before it builds or serves; only its activate+init+assert prelude runs.
        backend.run_backend = lambda args: _noop()
        try:
            import uvloop

            uvloop.run = lambda coro: coro.close()
        except ImportError:
            pass
        asyncio.run = lambda coro: coro.close()

        result = CliRunner().invoke(backend.main, ["worker"], catch_exceptions=False)
        assert result.exit_code == 0, result.output

        from tai42_skeleton.routers.metrics_settings import metrics_settings

        d = metrics_settings().prometheus_multiproc_dir
        assert os.path.isdir(d), d
        print("DIR_CREATED")
        """
    )
    env = os.environ.copy()
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)  # a clean env, as production launches into
    env["TMPDIR"] = str(tmp_path)  # coded-default dir lands under tmp, not the host tempdir
    result = subprocess.run([sys.executable, "-c", child], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "DIR_CREATED" in result.stdout


def test_mcp_app_master_activates_multiproc_before_prometheus_import_in_clean_env(tmp_path) -> None:
    """``run_mcp_app`` (the mcp_app writer master) must call
    ``activate_multiproc_env()`` BEFORE the statement that imports the wipe helper
    from ``routers.prometheus`` (which pulls in ``prometheus_client`` and freezes its
    value backend). Same statement-order contract as the backend, checked in a FRESH
    interpreter with ``PROMETHEUS_MULTIPROC_DIR`` UNSET.

    ``run_stdio`` is stubbed so the master runs its env-stamp→activate→import→wipe
    prelude without building or serving; the child then asserts the mmap value class
    froze, which only holds if activation ran before the prometheus import. ``TMPDIR``
    is redirected into ``tmp_path`` so the master's real wipe touches tmp, not the
    host tempdir, while the env var itself stays unset so the freeze must come from
    activation."""
    child = textwrap.dedent(
        """
        import tai42_skeleton.cli.mcp_app as mcp_app
        from tai42_skeleton.settings.cache import app_args_settings

        async def _fake_run_stdio():
            return 0

        # Skip the real stdio serve (build_app + app_context); the activate→import→wipe
        # prelude ahead of it is what carries the ordering contract.
        mcp_app.run_stdio = _fake_run_stdio

        # stdio refuses a host/port that differs from the resolved defaults, so pass the
        # defaults through unchanged.
        defaults = app_args_settings()
        mcp_app.run_mcp_app(
            manifest_path="unused.yaml",
            transport="stdio",
            host=defaults.host,
            port=defaults.port,
            workers=1,
        )

        from prometheus_client.values import ValueClass

        assert ValueClass.__name__ == "MmapedValue", ValueClass.__name__
        print("MmapedValue")
        """
    )
    env = os.environ.copy()
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)  # a clean env, as production launches into
    env["TMPDIR"] = str(tmp_path)  # redirect the coded-default dir the real wipe touches
    result = subprocess.run([sys.executable, "-c", child], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "MmapedValue" in result.stdout


def test_lock_helpers_provide_real_mutual_exclusion(tmp_path) -> None:
    """The lock helpers are REAL exclusion, never a no-op: while one fd holds the
    posix exclusive lock, a second fd's non-blocking acquire fails. (The win32
    branch uses ``msvcrt.locking``, unimportable here, so this pins the posix
    path — the one that runs in this environment.)"""
    import fcntl

    lock_path = tmp_path / "x.lock"
    with open(lock_path, "w") as first:
        prom_mod._lock_exclusive(first.fileno())
        try:
            # A held exclusive lock blocks a second fd's non-blocking acquire.
            with open(lock_path, "w") as second, pytest.raises(BlockingIOError):
                fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            prom_mod._unlock(first.fileno())


# --- mode-aware render fork -------------------------------------------------


def test_render_metrics_multiproc_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list = []
    monkeypatch.setattr(prom_mod, "multiproc_active", lambda: True)
    monkeypatch.setattr(prom_mod, "render_multiproc_metrics", lambda: called.append(True) or b"MP")

    assert prom_mod.render_metrics() == b"MP"
    assert called == [True]


def test_render_metrics_in_process_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prom_mod, "multiproc_active", lambda: False)
    monkeypatch.setattr(
        prom_mod, "generate_latest", lambda registry: b"INPROC" if registry is prom_mod.REGISTRY else b"WRONG"
    )

    # The in-process branch renders the module-global default registry.
    assert prom_mod.render_metrics() == b"INPROC"


def test_render_metrics_logs_mode_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # The served mode is announced exactly once — on the first scrape, naming the
    # mode and the frozen value backend that decided it — never per scrape.
    monkeypatch.setattr(prom_mod, "multiproc_active", lambda: True)
    monkeypatch.setattr(prom_mod, "render_multiproc_metrics", lambda: b"MP")
    monkeypatch.setattr(prom_mod, "_mode_logged", False)

    with caplog.at_level(logging.INFO, logger="tai42_skeleton.routers.prometheus"):
        prom_mod.render_metrics()
        first = [r for r in caplog.records if "/metrics in" in r.getMessage()]
        assert len(first) == 1
        assert "multiproc" in first[0].getMessage()

        caplog.clear()
        prom_mod.render_metrics()
        assert not [r for r in caplog.records if "/metrics in" in r.getMessage()]
