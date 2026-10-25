"""FileConfigManager read/write coverage over a faked filesystem (``tmp_path``).

Exercises the path resolution (constructor arg / ``TAI_CONFIG_DIR_PATH`` / ``/app``
default and the ``TAI_MANIFEST_PATH`` override), the ``.env`` read/write merge
(preserve + drop-empty), and the manifest read plus the ``mutate_manifest`` /
``replace_manifest`` write seams (``!ENV`` expansion vs preservation,
missing-file behavior, defaults backfill)."""

from __future__ import annotations

import asyncio
import errno
import multiprocessing
import multiprocessing.connection
import os
import select
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event

import pytest
import yaml
from tests.config._file_manager_children import (
    LockTraffic,
    env_writer,
    hold_write_span,
    mutate_writer,
    single_env_write,
    take_write_span,
)

import tai42_skeleton.config.file_manager as fm
from tai42_skeleton.app.reload_gate import GateParty, hold_recorded, hold_recorded_blocking
from tai42_skeleton.config.file_manager import FileConfigManager

# --- spawned-child start-up and exit ------------------------------------------
#
# A spawned child spends its start-up in a fresh interpreter importing the children module,
# which takes host time only. So the start-up is awaited on events: each child reports ready
# on a pipe just before its locked work, and a child that exits before reporting fails the test
# at once with its exit code. A child that stays alive and never reports fails at the start-up
# bound.

# The start-up bound: twice the slowest measured start-up (2 s on an idle host, 21 s at load
# 276, 87.8 s for a two-writer test in a full suite at load 250-300), rounded up to the next
# 10 s.
_START_UP_BOUND_S = 180.0

# The work a child does after it reports ready: at most two writers' serialized
# read-merge-write rounds (ten rounds). Measured at most 0.90 s with sixteen writers racing at
# load 276.
_LOCKED_PHASE_BOUND_S = 10.0


def _spawn_reporting(ctx: SpawnContext, target: Callable[..., None], args: tuple) -> tuple[BaseProcess, Connection]:
    """Start ``target(*args, ready)`` in a spawned process; return it and the ready pipe's read end."""
    reader, writer = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=target, args=(*args, writer))
    proc.start()
    # Only the child keeps the write end, so the read end sees end-of-file the moment the child
    # exits without reporting ready.
    writer.close()
    return proc, reader


def _await_ready(*started: tuple[str, BaseProcess, Connection]) -> None:
    """Wait until every child reported ready; fail at once when one exits first, at the bound when one never reports."""
    pending = {reader: (name, proc) for name, proc, reader in started}
    deadline = time.monotonic() + _START_UP_BOUND_S
    while pending:
        woke = multiprocessing.connection.wait(list(pending), timeout=max(0.0, deadline - time.monotonic()))
        if not woke:
            names = ", ".join(name for name, _proc in pending.values())
            pytest.fail(f"{names} did not report ready within {_START_UP_BOUND_S} s of starting")
        for reader in [r for r in pending if r in woke]:
            name, proc = pending.pop(reader)
            try:
                reader.recv()
            except EOFError:
                proc.join()
                pytest.fail(f"{name} exited with code {proc.exitcode} before it reported ready")
            finally:
                reader.close()


def _join_after_ready(*procs: tuple[str, BaseProcess]) -> None:
    """Join every child within ``_LOCKED_PHASE_BOUND_S``; fail naming one still running or exiting non-zero."""
    deadline = time.monotonic() + _LOCKED_PHASE_BOUND_S
    for _name, proc in procs:
        proc.join(timeout=max(0.0, deadline - time.monotonic()))
    for name, proc in procs:
        assert proc.exitcode is not None, f"{name} still running {_LOCKED_PHASE_BOUND_S} s after it reported ready"
        assert proc.exitcode == 0, f"{name} exited with code {proc.exitcode}"


def _stop_leftovers(*procs: BaseProcess) -> None:
    for proc in procs:
        if proc.is_alive():
            proc.terminate()
            proc.join()


def _run_two_writers(target, args_a: tuple, args_b: tuple) -> None:
    """Run ``target`` in two spawned processes, released together by a barrier once both started."""
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    counts = ctx.Array("i", 6)
    proc_a, ready_a = _spawn_reporting(ctx, target, (*args_a, barrier, LockTraffic(counts, 0)))
    proc_b, ready_b = _spawn_reporting(ctx, target, (*args_b, barrier, LockTraffic(counts, 1)))
    try:
        _await_ready(("writer A", proc_a, ready_a), ("writer B", proc_b, ready_b))
        _join_after_ready(("writer A", proc_a), ("writer B", proc_b))
    finally:
        _stop_leftovers(proc_a, proc_b)


# --- path resolution --------------------------------------------------------


def test_default_dir_is_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_CONFIG_DIR_PATH", raising=False)
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    mgr = FileConfigManager()
    assert mgr.env_path == os.path.join("/app", ".env")
    assert mgr.manifest_path == os.path.join("/app", "manifest.yml")
    assert mgr._defaults_manifest_path == os.path.join("/app", "templates", "manifest.yml")


def test_config_dir_path_env_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(tmp_path))
    mgr = FileConfigManager()
    assert mgr.env_path == os.path.join(str(tmp_path), ".env")


def test_constructor_arg_wins(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr.env_path.startswith(str(tmp_path))


def test_tai_manifest_path_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    override = str(tmp_path / "custom.yml")
    monkeypatch.setenv("TAI_MANIFEST_PATH", override)
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr.manifest_path == override


def test_an_empty_tai_manifest_path_is_absent(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TAI_MANIFEST_PATH", "")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr.manifest_path == os.path.join(str(tmp_path), "manifest.yml")


def test_tai_manifest_path_is_read_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    monkeypatch.setenv("TAI_MANIFEST_PATH", str(tmp_path / "first.yml"))
    assert mgr.manifest_path == str(tmp_path / "first.yml")
    monkeypatch.setenv("TAI_MANIFEST_PATH", str(tmp_path / "second.yml"))
    assert mgr.manifest_path == str(tmp_path / "second.yml")


# --- env read/write ---------------------------------------------------------


def test_read_env_missing_raises(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="Env file not found"):
        mgr.read_env()


def test_read_env_parses_and_drops_valueless(tmp_path) -> None:
    # ``BARE`` (no ``=``) parses to None; ``EMPTY=`` parses to "".
    (tmp_path / ".env").write_text("A=1\nB=two\nEMPTY=\nBARE\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    parsed = mgr.read_env()
    assert parsed["A"] == "1"
    assert parsed["B"] == "two"
    # Valueless (None) keys are filtered out; an explicit empty string is kept.
    assert "BARE" not in parsed
    assert parsed["EMPTY"] == ""


def test_write_env_creates_file_when_absent(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"A": "1", "B": "2"})
    # Values are written as double-quoted dotenv literals so they round-trip.
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert text == 'A="1"\nB="2"\n'
    assert mgr.read_env() == {"A": "1", "B": "2"}


def test_write_env_round_trips_tricky_values(tmp_path) -> None:
    """A newline, ``#``, quote, and leading/trailing space survive a write→read
    cycle instead of injecting a key or being silently truncated."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    tricky = {
        "MULTILINE": "value\nINJECTED=1",
        "HASH": "hello #world",
        "QUOTED": 'has "quotes"',
        "SPACED": "  padded  ",
        "PEM": "-----BEGIN-----\nkeybody\n-----END-----",
    }
    mgr.write_env(tricky)
    assert mgr.read_env() == tricky


def test_write_env_rejects_malformed_key(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(ValueError, match="invalid env key"):
        mgr.write_env({"BAD KEY": "1"})
    with pytest.raises(ValueError, match="invalid env key"):
        mgr.write_env({"A\nB": "1"})


def test_write_env_round_trips_dollar_values_without_interpolation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Values containing ``$``, ``$var`` and ``${OTHER}`` survive a write→read
    cycle byte-identically — ``read_env`` must NOT POSIX-interpolate them, even
    when the referenced var exists in the process environment."""
    # Set OTHER in the environment to prove it is NOT substituted on read.
    monkeypatch.setenv("OTHER", "LEAKED")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    values = {
        "DOLLAR": "a$b",
        "SHELL_VAR": "prefix-$var-suffix",
        "BRACED": "before ${OTHER} after",
    }
    mgr.write_env(values)
    assert mgr.read_env() == values


def test_write_env_round_trips_a_backslash_ending_value_preserving_existing_store(tmp_path) -> None:
    """A backslash-ending Windows-path value round-trips through the double-quoted
    ``.env`` literal, so ``write_env`` succeeds: the value is read back byte-for-byte
    and a pre-existing store key is preserved by the merge."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    # Pre-existing store that the merge-write must preserve.
    mgr.write_env({"KEEP": "safe"})

    mgr.write_env({"WINPATH": "C:\\data\\"})

    assert mgr.read_env() == {"KEEP": "safe", "WINPATH": "C:\\data\\"}


def test_write_env_round_trips_a_backslash_ending_value(tmp_path) -> None:
    """With no pre-existing store, a backslash-ending value writes the ``.env`` file
    and reads back exactly."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"WINPATH": "C:\\data\\"})
    assert (tmp_path / ".env").exists()
    assert mgr.read_env() == {"WINPATH": "C:\\data\\"}


def test_write_env_round_trips_a_backslash_ending_value_with_other_keys(tmp_path) -> None:
    """A backslash-ending value round-trips in either key position — written last
    (only the guard's trailing sentinel sits after it) or followed by another key —
    so every key reads back exactly."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    # Backslash-ending value in the LAST position.
    mgr.write_env({"GOOD": "ok", "WINPATH": "C:\\data\\"})
    assert mgr.read_env() == {"GOOD": "ok", "WINPATH": "C:\\data\\"}

    # Same value NOT last (a good key follows it) also round-trips.
    other = FileConfigManager(config_dir_path=str(tmp_path / "other"))
    (tmp_path / "other").mkdir()
    other.write_env({"WINPATH": "C:\\data\\", "GOOD": "ok"})
    assert other.read_env() == {"WINPATH": "C:\\data\\", "GOOD": "ok"}


def test_write_env_round_trip_guard_raises_on_a_value_that_does_not_parse_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The defensive round-trip guard fails loudly if a serialized value does not
    parse back to itself. With the serializer forced to emit a literal that
    ``dotenv_values`` reads back as a different string, ``write_env`` raises
    ``ValueError`` naming the key and writes no file."""
    monkeypatch.setattr(fm, "_dotenv_serialize_value", lambda value: '"not-the-value"')
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(ValueError, match=r"WINPATH.*cannot be round-tripped"):
        mgr.write_env({"WINPATH": "real-value"})
    assert not (tmp_path / ".env").exists()


def test_write_env_round_trips_even_backslash_and_tricky_values(tmp_path) -> None:
    """The round-trip guard is not over-strict: a value with an even number of
    internal backslashes plus the usual tricky characters still write and read
    back byte-identically."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    values = {
        "EVEN_BACKSLASH": "a\\b",
        "MULTILINE": "line\nINJECTED=1",
        "QUOTED": 'has "quotes"',
        "HASH": "trailing #comment",
        "DOLLAR": "a$b ${OTHER}",
        "SPACED": "  padded  ",
    }
    mgr.write_env(values)
    assert mgr.read_env() == values


def test_write_env_rejects_key_with_trailing_newline(tmp_path) -> None:
    """A key ending in a newline (or containing a space) is rejected before it can
    inject a stray line into ``.env``."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(ValueError, match="invalid env key"):
        mgr.write_env({"A\n": "1"})
    with pytest.raises(ValueError, match="invalid env key"):
        mgr.write_env({"A B": "1"})


def test_write_env_preserves_key_order_across_updates(tmp_path) -> None:
    """Updating an existing key rewrites its value in place instead of moving it
    to the top, so the on-disk key order stays stable."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"A": "1", "B": "2", "C": "3"})
    mgr.write_env({"B": "new"})
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    keys = [line.split("=", 1)[0] for line in text.splitlines() if line]
    assert keys == ["A", "B", "C"]
    assert mgr.read_env() == {"A": "1", "B": "new", "C": "3"}


def test_write_env_is_mode_0600(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"A": "1"})
    assert (os.stat(tmp_path / ".env").st_mode & 0o777) == 0o600


def test_write_env_preserves_existing_unrelated_keys(tmp_path) -> None:
    (tmp_path / ".env").write_text("KEEP=old\nA=stale\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"A": "new"})
    parsed = mgr.read_env()
    # Provided key overwritten; unrelated key preserved.
    assert parsed["A"] == "new"
    assert parsed["KEEP"] == "old"


def test_write_env_drops_empty_and_none_values(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"A": "1", "B": "", "C": None})  # type: ignore[dict-item]
    parsed = mgr.read_env()
    assert parsed == {"A": "1"}


def test_write_env_empty_merge_writes_no_trailing_newline(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({})
    assert (tmp_path / ".env").read_text(encoding="utf-8") == ""


# --- replace_env seam -------------------------------------------------------


def test_replace_env_deletes_uninvited_keys(tmp_path) -> None:
    (tmp_path / ".env").write_text("KEEP=old\nDROP=gone\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    # Whole-map replace: DROP, present before but absent from the new map, is gone.
    mgr.replace_env({"KEEP": "new", "ADD": "added"})
    assert mgr.read_env() == {"KEEP": "new", "ADD": "added"}


def test_replace_env_drops_empty_and_none_values(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.replace_env({"A": "1", "B": "", "C": None})  # type: ignore[dict-item]
    assert mgr.read_env() == {"A": "1"}


def test_replace_env_rejects_malformed_key(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(ValueError, match="invalid env key"):
        mgr.replace_env({"BAD KEY": "1"})


def test_replace_env_round_trips_a_backslash_ending_value(tmp_path) -> None:
    """A whole-map replace of a backslash-ending value writes the ``.env`` file and
    reads back exactly, with only the replaced key stored."""
    (tmp_path / ".env").write_text("DROP=gone\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.replace_env({"WINPATH": "C:\\data\\"})
    # Whole-map replace: the prior key is gone and the new value reads back exactly.
    assert mgr.read_env() == {"WINPATH": "C:\\data\\"}


def test_replace_env_atomic_write_cleans_up_temp_on_replace_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """When ``os.replace`` fails mid-write, the error propagates loudly, the orphan
    temp is removed, and the prior ``.env`` is left intact (atomic — never a
    half-written store)."""
    import tai42_skeleton.config.file_manager as fm

    (tmp_path / ".env").write_text('PRIOR="kept"\n', encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(fm.os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        mgr.replace_env({"A": "1"})

    # The prior store is untouched and no orphan temp lingers (only ``.env`` + its
    # persistent flock sidecar remain).
    assert mgr.read_env() == {"PRIOR": "kept"}
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", ".env.lock"]


# --- manifest read ----------------------------------------------------------


def test_read_manifest_missing_raises(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="Manifest not found"):
        mgr.read_manifest()


def test_read_manifest_expands_env_tags(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MY_SECRET", "resolved-value")
    (tmp_path / "manifest.yml").write_text("token: !ENV ${MY_SECRET}\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr.read_manifest()["token"] == "resolved-value"


def test_read_manifest_preserved_keeps_env_marker_and_hides_secret(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The preserved-tag view keeps each ``!ENV`` node as its literal marker string —
    # JSON-safe, and the resolved secret is never read.
    monkeypatch.setenv("SOME_VAR", "super-secret-value")
    (tmp_path / "manifest.yml").write_text("key: !ENV ${SOME_VAR}\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    preserved = mgr.read_manifest_preserved()
    assert preserved == {"key": "!ENV ${SOME_VAR}"}
    import json

    assert "super-secret-value" not in json.dumps(preserved)


def test_read_manifest_preserved_missing_raises(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="Manifest not found"):
        mgr.read_manifest_preserved()


def test_manifest_preserved_export_import_fresh_host_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Fresh-host restore: the preserved view exported from one host imports into an
    # EMPTY config dir as a raw ``!ENV`` tag (not the baked secret), and the runtime
    # read then resolves it from env — the secret never touches disk.
    monkeypatch.setenv("SOME_VAR", "super-secret-value")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "manifest.yml").write_text("key: !ENV ${SOME_VAR}\n", encoding="utf-8")
    source = FileConfigManager(config_dir_path=str(source_dir))
    exported = source.read_manifest_preserved()

    # A brand-new host with no existing manifest.
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = FileConfigManager(config_dir_path=str(target_dir))
    # Fresh-host import: replace_manifest persists the preserved-view document
    # verbatim (the caller built it from ``read_manifest_preserved``).
    target.replace_manifest(exported)

    raw = (target_dir / "manifest.yml").read_text(encoding="utf-8")
    assert "!ENV" in raw
    assert "super-secret-value" not in raw
    # The runtime view resolves the placeholder from env.
    assert target.read_manifest()["key"] == "super-secret-value"


def test_read_defaults_manifest_absent_returns_empty(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr.read_defaults_manifest() == {}


def test_read_defaults_manifest_expands(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("DEF_VAL", "xyz")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "manifest.yml").write_text("k: !ENV ${DEF_VAL}\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr.read_defaults_manifest() == {"k": "xyz"}


def test_read_defaults_manifest_bad_yaml_raises(tmp_path) -> None:
    """A malformed defaults file raises rather than degrading to ``{}`` — a
    broken template must surface, not silently drop defaults from the write merge."""
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "manifest.yml").write_text("a: [unterminated\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(yaml.YAMLError):
        mgr.read_defaults_manifest()


# --- read/write error propagation, probe & atomic write ---------------------


def test_read_env_os_error_propagates(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A read failure from the parser is logged and re-raised, never swallowed
    into an empty config."""
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")  # exists() True
    import tai42_skeleton.config.file_manager as fm

    def boom(path, interpolate=True):
        raise OSError("disk error")

    monkeypatch.setattr(fm, "dotenv_values", boom)
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(OSError, match="disk error"):
        mgr.read_env()


def test_write_env_os_error_propagates(tmp_path) -> None:
    """A write failure (parent dir missing) is logged and re-raised."""
    missing = tmp_path / "nope"  # never created -> open() raises FileNotFoundError
    mgr = FileConfigManager(config_dir_path=str(missing))
    with pytest.raises(FileNotFoundError):
        mgr.write_env({"A": "1"})


def test_write_env_allows_probe_named_key(tmp_path) -> None:
    """A caller may write a key literally named ``_TAI_ENV_ROUNDTRIP_PROBE``: the
    round-trip guard derives a non-colliding probe key, so the value is not shadowed
    by the sentinel and falsely flagged. It writes and reads back intact."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"_TAI_ENV_ROUNDTRIP_PROBE": "real-value", "OTHER": "x"})
    parsed = mgr.read_env()
    assert parsed["_TAI_ENV_ROUNDTRIP_PROBE"] == "real-value"
    assert parsed["OTHER"] == "x"


def test_atomic_write_cleans_up_temp_on_replace_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """When ``os.replace`` fails mid-write, the error propagates loudly AND the
    orphan temp file is removed, so no leftover temp lingers in the config dir
    (the ``except OSError: os.unlink(tmp)`` branch ran)."""
    import tai42_skeleton.config.file_manager as fm

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(fm.os, "replace", boom)
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(OSError, match="replace failed"):
        mgr.write_env({"A": "1"})

    # The error propagated and no orphan temp file remains in the config dir. Only
    # the persistent flock sidecar (``.env.lock``) may remain — it is the lock
    # subject, not a temp, and is never unlinked.
    assert not (tmp_path / ".env").exists()
    assert [p.name for p in tmp_path.iterdir()] == [".env.lock"]


# --- mutate_manifest seam ---------------------------------------------------


def test_mutate_manifest_edits_in_place_and_persists(tmp_path) -> None:
    (tmp_path / "manifest.yml").write_text("a: 1\nb: 2\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))

    def mutator(doc: dict) -> None:
        doc["a"] = 99
        doc["c"] = 3

    result = mgr.mutate_manifest(mutator)
    assert result["a"] == 99
    assert result["c"] == 3
    reread = mgr.read_manifest()
    assert reread == {"a": 99, "b": 2, "c": 3}


def test_mutate_manifest_preserves_comments(tmp_path) -> None:
    """The round-trip read/dump keeps hand-authored comments on untouched keys."""
    source = "# top comment\na: 1  # inline on a\nb: 2  # inline on b\n"
    (tmp_path / "manifest.yml").write_text(source, encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.mutate_manifest(lambda doc: doc.__setitem__("a", 5))
    raw = (tmp_path / "manifest.yml").read_text(encoding="utf-8")
    assert "# top comment" in raw
    assert "# inline on b" in raw
    assert "a: 5" in raw


def test_mutate_manifest_creates_manifest_when_absent(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.mutate_manifest(lambda doc: doc.__setitem__("fresh", 1))
    assert mgr.read_manifest() == {"fresh": 1}


def test_mutate_manifest_mutator_exception_aborts_with_nothing_written(tmp_path) -> None:
    (tmp_path / "manifest.yml").write_text("a: 1\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))

    def boom(doc: dict) -> None:
        doc["a"] = 2
        raise ValueError("mutator failed")

    with pytest.raises(ValueError, match="mutator failed"):
        mgr.mutate_manifest(boom)
    # Nothing was written: the original content is intact.
    assert (tmp_path / "manifest.yml").read_text(encoding="utf-8") == "a: 1\n"


def test_mutate_manifest_reads_preserved_view_and_keeps_secret_tagged(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The mutator sees the ``!ENV`` marker (never the resolved secret), and an
    untouched marker is written back as its placeholder, not baked to disk."""
    monkeypatch.setenv("SOME_VAR", "super-secret-value")
    (tmp_path / "manifest.yml").write_text("token: !ENV ${SOME_VAR}\nother: 1\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))

    seen: dict = {}

    def mutator(doc: dict) -> None:
        seen["token"] = doc["token"]
        doc["other"] = 2

    result = mgr.mutate_manifest(mutator)
    assert seen["token"] == "!ENV ${SOME_VAR}"
    assert result["token"] == "!ENV ${SOME_VAR}"
    raw = (tmp_path / "manifest.yml").read_text(encoding="utf-8")
    assert "!ENV" in raw
    assert "super-secret-value" not in raw
    # The runtime view still resolves the untouched placeholder from env.
    assert mgr.read_manifest()["token"] == "super-secret-value"


def test_mutate_manifest_backfills_defaults(tmp_path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "manifest.yml").write_text("from_defaults: 1\n", encoding="utf-8")
    (tmp_path / "manifest.yml").write_text("from_current: 2\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.mutate_manifest(lambda doc: doc.__setitem__("added", 3))
    merged = mgr.read_manifest()
    assert merged == {"from_defaults": 1, "from_current": 2, "added": 3}


# --- replace_manifest seam --------------------------------------------------


def test_replace_manifest_deletes_absent_keys(tmp_path) -> None:
    """A replace is a true replace: a key absent from the document is DELETED
    (unlike the three-way write merge, which backfills old keys)."""
    (tmp_path / "manifest.yml").write_text("keep: 1\ndrop: 2\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    result = mgr.replace_manifest({"keep": 10})
    assert result == {"keep": 10}
    assert mgr.read_manifest() == {"keep": 10}


def test_replace_manifest_backfills_defaults_only(tmp_path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "manifest.yml").write_text("from_defaults: 1\n", encoding="utf-8")
    (tmp_path / "manifest.yml").write_text("old: 9\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.replace_manifest({"new": 2})
    merged = mgr.read_manifest()
    # Defaults backfill the missing key; the old document's key is gone.
    assert merged == {"from_defaults": 1, "new": 2}


def test_replace_manifest_does_not_mutate_caller_dict(tmp_path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "manifest.yml").write_text("from_defaults: 1\n", encoding="utf-8")
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    document = {"new": 2}
    mgr.replace_manifest(document)
    # The backfilled default landed in the persisted copy, not the caller's dict.
    assert document == {"new": 2}


def test_replace_manifest_persists_env_marker_verbatim(tmp_path) -> None:
    """The caller builds the document from the preserved view; a marker string is
    dumped back as a genuine ``!ENV`` tag, so no secret bakes to disk."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.replace_manifest({"token": "!ENV ${SOME_VAR}"})
    raw = (tmp_path / "manifest.yml").read_text(encoding="utf-8")
    assert "!ENV ${SOME_VAR}" in raw
    assert mgr.read_manifest_preserved() == {"token": "!ENV ${SOME_VAR}"}


def test_replace_manifest_preserves_comments_from_round_trip_document(tmp_path) -> None:
    """A comment-bearing round-trip document (the preserved-read product) is
    persisted verbatim: comments and ``!ENV`` markers survive the replace, so the
    caller-owned round-trip view is not silently flattened to a plain mapping."""
    (tmp_path / "manifest.yml").write_text(
        "# leading comment\ntoken: !ENV ${SOME_VAR}  # inline comment\n",
        encoding="utf-8",
    )
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    preserved = mgr.read_manifest_preserved()
    mgr.replace_manifest(preserved)
    raw = (tmp_path / "manifest.yml").read_text(encoding="utf-8")
    assert "# leading comment" in raw
    assert "# inline comment" in raw
    assert "!ENV ${SOME_VAR}" in raw


# --- cross-process flock: concurrent lost-update guard ----------------------


def test_write_env_concurrent_processes_keep_every_key_with_lock(tmp_path) -> None:
    """Two spawned processes each ``write_env`` disjoint key batches against the same
    dir, the second contending for the flock inside the first one's read-merge-write. The
    flock serializes the rounds, so every key from both survives."""
    (tmp_path / ".env").write_text("", encoding="utf-8")  # exists so read_env re-reads
    _run_two_writers(env_writer, (str(tmp_path), "a", 5, True), (str(tmp_path), "b", 5, True))
    result = FileConfigManager(config_dir_path=str(tmp_path)).read_env()
    expected = {f"a{i}": str(i) for i in range(5)} | {f"b{i}": str(i) for i in range(5)}
    assert result == expected


def test_write_env_concurrent_processes_lose_update_without_lock(tmp_path) -> None:
    """Without the flock, two writers whose reads both land before either write merge
    against the same base, so one round's update is lost: what the flock prevents."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    _run_two_writers(env_writer, (str(tmp_path), "a", 5, False), (str(tmp_path), "b", 5, False))
    result = FileConfigManager(config_dir_path=str(tmp_path)).read_env()
    expected = {f"a{i}": str(i) for i in range(5)} | {f"b{i}": str(i) for i in range(5)}
    assert result != expected


def test_mutate_manifest_concurrent_processes_keep_both_edits_with_lock(tmp_path) -> None:
    """Two spawned processes each ``mutate_manifest`` a disjoint key, the second contending
    for the transaction lock inside the first one's read → mutate → write. The lock spans the
    whole span, so both edits land."""
    (tmp_path / "manifest.yml").write_text("seed: 0\n", encoding="utf-8")
    _run_two_writers(mutate_writer, (str(tmp_path), "alpha", True), (str(tmp_path), "beta", True))
    result = FileConfigManager(config_dir_path=str(tmp_path)).read_manifest()
    assert result["alpha"] == "alpha"
    assert result["beta"] == "beta"


def test_mutate_manifest_concurrent_processes_lose_update_without_lock(tmp_path) -> None:
    """Without the lock, two mutations whose reads both land before either write merge
    against the same base, so one edit is lost: the lost update the transaction lock closes."""
    (tmp_path / "manifest.yml").write_text("seed: 0\n", encoding="utf-8")
    _run_two_writers(mutate_writer, (str(tmp_path), "alpha", False), (str(tmp_path), "beta", False))
    result = FileConfigManager(config_dir_path=str(tmp_path)).read_manifest()
    assert not ({"alpha", "beta"} <= set(result))


# --- cross-process flock: sidecar mechanics ---------------------------------


def test_lock_sidecar_created_0600_next_to_target(tmp_path) -> None:
    """The lock sidecar is created ``0600`` beside the target, and the target's own
    content is unaffected by the sidecar's presence."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"A": "1"})
    lock = tmp_path / ".env.lock"
    assert lock.exists()
    assert (os.stat(lock).st_mode & 0o777) == 0o600
    assert (tmp_path / ".env").read_text(encoding="utf-8") == 'A="1"\n'


def test_write_env_blocks_on_held_sidecar_lock(tmp_path) -> None:
    """A ``write_env`` in a child process blocks while the SIDECAR lock is held by the
    test, and completes only after it is released — proving the write serializes on
    the sidecar lock."""
    import fcntl

    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    fd = os.open(f"{mgr.env_path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ctx = multiprocessing.get_context("spawn")
    child, ready = _spawn_reporting(ctx, single_env_write, (str(tmp_path), "A", "1"))
    try:
        _await_ready(("the writer", child, ready))
        child.join(timeout=2.0)
        # While the sidecar lock is held the child cannot acquire it — it is still
        # running and has written nothing.
        assert child.is_alive()
        assert not (tmp_path / ".env").exists()
        # Release the lock; the child now acquires it and completes the write.
        fcntl.flock(fd, fcntl.LOCK_UN)
        _join_after_ready(("the writer", child))
        assert FileConfigManager(config_dir_path=str(tmp_path)).read_env() == {"A": "1"}
    finally:
        _stop_leftovers(child)
        os.close(fd)


def test_write_env_locks_the_sidecar_not_the_target(tmp_path) -> None:
    """Holding a lock on the TARGET ``.env`` file itself does NOT block a child's
    ``write_env`` — the manager locks the sidecar, never the target (a lock on the
    target would be swapped out from under by ``os.replace`` anyway)."""
    import fcntl

    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"SEED": "0"})  # create the target so it can be locked
    fd = os.open(mgr.env_path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ctx = multiprocessing.get_context("spawn")
    child, ready = _spawn_reporting(ctx, single_env_write, (str(tmp_path), "A", "1"))
    try:
        _await_ready(("the writer", child, ready))
        # The child locked the SIDECAR (which we do not hold), so it completes despite
        # our lock on the target.
        _join_after_ready(("the writer", child))
        assert FileConfigManager(config_dir_path=str(tmp_path)).read_env()["A"] == "1"
    finally:
        _stop_leftovers(child)
        os.close(fd)


def test_windows_lock_path_refuses_the_write(monkeypatch, tmp_path) -> None:
    """On Windows ``fcntl`` is unavailable, so a serialized write cannot be
    guaranteed and the write is REFUSED with a clear error (not a warn-and-proceed
    no-op). Simulated by faking the module's ``sys.platform`` (the real lock path is
    POSIX-only and untestable on Linux)."""
    import types

    monkeypatch.setattr(fm, "sys", types.SimpleNamespace(platform="win32"))
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(RuntimeError, match="POSIX"):
        mgr.write_env({"A": "1"})
    # The write is refused before anything lands: no file is created.
    assert not (tmp_path / ".env").exists()


def test_windows_lock_path_refuses_manifest_seams(monkeypatch, tmp_path) -> None:
    """The Windows refusal covers every manifest writer — ``mutate_manifest`` and
    ``replace_manifest`` both raise before writing."""
    import types

    monkeypatch.setattr(fm, "sys", types.SimpleNamespace(platform="win32"))
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(RuntimeError, match="POSIX"):
        mgr.mutate_manifest(lambda doc: doc.__setitem__("a", 1))
    with pytest.raises(RuntimeError, match="POSIX"):
        mgr.replace_manifest({"a": 1})
    assert not (tmp_path / "manifest.yml").exists()


# --- the store-wide write span ----------------------------------------------

# How long a child that must stay blocked on the span is watched before the test releases it.
_BLOCKED_WATCH_S = 1.0


def _hold_span_in_child(ctx: SpawnContext, config_dir: str) -> tuple[BaseProcess, Event]:
    """Start a child holding ``exclusive_write`` on *config_dir*; return it, inside the span, with its release event."""
    entered, release = ctx.Event(), ctx.Event()
    holder, ready = _spawn_reporting(ctx, hold_write_span, (config_dir, entered, release))
    _await_ready(("the holder", holder, ready))
    assert entered.wait(_LOCKED_PHASE_BOUND_S), "the holder did not enter the span"
    return holder, release


def test_the_write_span_excludes_a_second_process_until_the_holder_leaves(tmp_path) -> None:
    ctx = multiprocessing.get_context("spawn")
    holder, release = _hold_span_in_child(ctx, str(tmp_path))
    acquired = ctx.Event()
    taker, ready = _spawn_reporting(ctx, take_write_span, (str(tmp_path), acquired))
    try:
        _await_ready(("the taker", taker, ready))
        assert not acquired.wait(_BLOCKED_WATCH_S), "a second process entered the span while it was held"
        release.set()
        assert acquired.wait(_LOCKED_PHASE_BOUND_S)
        _join_after_ready(("the holder", holder), ("the taker", taker))
    finally:
        _stop_leftovers(holder, taker)


def test_the_write_span_is_released_when_its_holder_dies(tmp_path) -> None:
    ctx = multiprocessing.get_context("spawn")
    holder, _release = _hold_span_in_child(ctx, str(tmp_path))
    acquired = ctx.Event()
    taker, ready = _spawn_reporting(ctx, take_write_span, (str(tmp_path), acquired))
    try:
        _await_ready(("the taker", taker, ready))
        assert not acquired.wait(_BLOCKED_WATCH_S)
        holder.kill()
        holder.join()
        assert acquired.wait(_LOCKED_PHASE_BOUND_S), "the span stayed held after its holder died"
        _join_after_ready(("the taker", taker))
    finally:
        _stop_leftovers(holder, taker)


def test_per_file_writes_inside_the_span_leave_it_held(tmp_path) -> None:
    """``write_env`` and ``mutate_manifest`` inside the span take and close their own
    per-file locks; the span stays held across them and its lock file is ``0600``."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.replace_manifest({"a": 1})
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    with mgr.exclusive_write():
        mgr.write_env({"A": "1"})
        mgr.mutate_manifest(lambda doc: doc.__setitem__("b", 2))
        taker, ready = _spawn_reporting(ctx, take_write_span, (str(tmp_path), acquired))
        try:
            _await_ready(("the taker", taker, ready))
            assert not acquired.wait(_BLOCKED_WATCH_S), "a per-file write inside the span released it"
        except BaseException:
            _stop_leftovers(taker)
            raise
    try:
        assert acquired.wait(_LOCKED_PHASE_BOUND_S)
        _join_after_ready(("the taker", taker))
    finally:
        _stop_leftovers(taker)
    assert mgr.read_env() == {"A": "1"}
    assert mgr.read_manifest() == {"a": 1, "b": 2}
    assert (os.stat(tmp_path / ".config-write.lock").st_mode & 0o777) == 0o600


def test_a_raw_write_from_another_process_does_not_wait_for_the_span(tmp_path) -> None:
    """The span excludes spans only: a sibling's ``write_env`` serializes on the env file's own lock."""
    ctx = multiprocessing.get_context("spawn")
    holder, release = _hold_span_in_child(ctx, str(tmp_path))
    writer, ready = _spawn_reporting(ctx, single_env_write, (str(tmp_path), "A", "1"))
    try:
        _await_ready(("the writer", writer, ready))
        _join_after_ready(("the writer", writer))
        assert FileConfigManager(config_dir_path=str(tmp_path)).read_env() == {"A": "1"}
        release.set()
        _join_after_ready(("the holder", holder))
    finally:
        _stop_leftovers(holder, writer)


def test_a_lock_file_close_that_fails_raises_from_the_exit_and_leaves_the_span_free(monkeypatch, tmp_path) -> None:
    lock_path = tmp_path / ".config-write.lock"
    real_close = os.close
    failed: list[int] = []

    def close(fd: int) -> None:
        is_lock = lock_path.exists() and os.path.samestat(os.fstat(fd), lock_path.stat())
        real_close(fd)
        if is_lock and not failed:
            failed.append(fd)
            raise OSError(errno.EIO, "closing the lock file failed")

    monkeypatch.setattr(fm.os, "close", close)
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(OSError, match="closing the lock file failed"), mgr.exclusive_write():
        mgr.write_env({"A": "1"})
    assert failed, "the span's exit never closed its lock file"
    assert mgr.read_env() == {"A": "1"}

    # The descriptor, and with it the record lock, is gone: another process and this one enter at once.
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    taker, ready = _spawn_reporting(ctx, take_write_span, (str(tmp_path), acquired))
    try:
        _await_ready(("the taker", taker, ready))
        assert acquired.wait(_LOCKED_PHASE_BOUND_S), "the span stayed held after its lock file failed to close"
        _join_after_ready(("the taker", taker))
    finally:
        _stop_leftovers(taker)
    with mgr.exclusive_write():
        pass


def test_the_write_span_raises_when_its_lock_file_cannot_be_opened(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path / "absent"))
    with pytest.raises(FileNotFoundError), mgr.exclusive_write():
        pytest.fail("the span was entered without its lock file")


def test_windows_lock_path_refuses_the_write_span(monkeypatch, tmp_path) -> None:
    import types

    monkeypatch.setattr(fm, "sys", types.SimpleNamespace(platform="win32"))
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(RuntimeError, match="POSIX"), mgr.exclusive_write():
        pytest.fail("the span was entered on Windows")


def test_two_threads_of_one_process_hold_the_write_span_one_at_a_time(tmp_path) -> None:
    """A record lock does not exclude its own process; the span still admits one thread at a time."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            with mgr.exclusive_write():
                entered.set()
                if not release.wait(_LOCKED_PHASE_BOUND_S):
                    raise TimeoutError("the test never released the first span")
        except BaseException as exc:
            errors.append(exc)

    def take() -> None:
        with FileConfigManager(config_dir_path=str(tmp_path)).exclusive_write():
            acquired.set()

    first = threading.Thread(target=hold)
    second = threading.Thread(target=take)
    first.start()
    try:
        assert entered.wait(_LOCKED_PHASE_BOUND_S), "the first thread did not enter the span"
        second.start()
        assert not acquired.wait(_BLOCKED_WATCH_S), "a second thread of the holding process entered the span"
    finally:
        release.set()
        first.join(_LOCKED_PHASE_BOUND_S)
    assert acquired.wait(_LOCKED_PHASE_BOUND_S), "the second thread did not enter the span after the first left it"
    second.join(_LOCKED_PHASE_BOUND_S)
    assert errors == []


def test_a_span_exited_on_another_thread_than_entered_it_is_released_for_every_holder(tmp_path) -> None:
    """The span is not bound to its entering thread: an exit from another thread releases both
    the record lock and this process's lock, so a third thread and then another process enter."""
    span = FileConfigManager(config_dir_path=str(tmp_path)).exclusive_write()
    span_entered, span_exited = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def enter_span() -> None:
        # Stays alive until the exit is done, so the exiting thread is a different live thread.
        span.__enter__()
        span_entered.set()
        span_exited.wait(_LOCKED_PHASE_BOUND_S)

    def exit_span() -> None:
        try:
            span.__exit__(None, None, None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            span_exited.set()

    entering = threading.Thread(target=enter_span)
    entering.start()
    try:
        assert span_entered.wait(_LOCKED_PHASE_BOUND_S), "the first thread did not enter the span"
        exiting = threading.Thread(target=exit_span)
        exiting.start()
        exiting.join(_LOCKED_PHASE_BOUND_S)
    finally:
        span_exited.set()
        entering.join(_LOCKED_PHASE_BOUND_S)
    assert errors == []

    ctx = multiprocessing.get_context("spawn")
    entered, release, acquired = threading.Event(), threading.Event(), ctx.Event()

    def hold() -> None:
        with FileConfigManager(config_dir_path=str(tmp_path)).exclusive_write():
            entered.set()
            release.wait(_LOCKED_PHASE_BOUND_S)

    third = threading.Thread(target=hold)
    third.start()
    taker: BaseProcess | None = None
    try:
        assert entered.wait(_LOCKED_PHASE_BOUND_S), "a third thread could not enter after a foreign-thread exit"
        taker, ready = _spawn_reporting(ctx, take_write_span, (str(tmp_path), acquired))
        _await_ready(("the taker", taker, ready))
        assert not acquired.wait(_BLOCKED_WATCH_S), "another process entered the span while the third thread held it"
        release.set()
        third.join(_LOCKED_PHASE_BOUND_S)
        assert acquired.wait(_LOCKED_PHASE_BOUND_S), "another process could not enter after the third thread left"
        _join_after_ready(("the taker", taker))
    finally:
        release.set()
        third.join(_LOCKED_PHASE_BOUND_S)
        _stop_leftovers(*([taker] if taker is not None else []))


async def test_a_waiter_cancelled_on_the_span_keeps_the_next_writer_of_its_process_excluding(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A span waiter cancelled while its thread blocks on the record lock leaves that thread to
    take and close the lock after the caller's queue moved on. The next writer of the same
    process still holds the span against every other process once it is inside."""
    ctx = multiprocessing.get_context("spawn")
    blocking = threading.Event()
    take_record_lock = fm._acquire_exclusive_record_lock

    def record_blocking(fd: int) -> None:
        blocking.set()
        take_record_lock(fd)

    monkeypatch.setattr(fm, "_acquire_exclusive_record_lock", record_blocking)
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    queue = asyncio.Lock()

    @asynccontextmanager
    async def write_exclusion() -> AsyncIterator[None]:
        async with (
            hold_recorded(queue, GateParty(request=None, task=None, task_epoch=None)) as party,
            hold_recorded_blocking(mgr.exclusive_write(), party),
        ):
            yield

    async def cancelled_writer() -> None:
        async with write_exclusion():
            pytest.fail("the body of a cancelled span waiter ran")

    inside, done = asyncio.Event(), asyncio.Event()

    async def next_writer() -> None:
        async with write_exclusion():
            inside.set()
            await done.wait()

    holder, release = _hold_span_in_child(ctx, str(tmp_path))
    taker: BaseProcess | None = None
    try:
        first = asyncio.create_task(cancelled_writer())
        assert await asyncio.to_thread(blocking.wait, _LOCKED_PHASE_BOUND_S), "the first writer never took the lock"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(next_writer())
        await asyncio.sleep(_BLOCKED_WATCH_S)
        assert not inside.is_set(), "the next writer entered while another process held the span"
        release.set()
        await asyncio.to_thread(_join_after_ready, ("the holder", holder))
        await asyncio.wait_for(inside.wait(), _LOCKED_PHASE_BOUND_S)
        acquired = ctx.Event()
        taker, ready = _spawn_reporting(ctx, take_write_span, (str(tmp_path), acquired))
        await asyncio.to_thread(_await_ready, ("the taker", taker, ready))
        held_out = not await asyncio.to_thread(acquired.wait, _BLOCKED_WATCH_S)
        done.set()
        await second
        assert held_out, "another process entered the span while the next writer held it"
        assert await asyncio.to_thread(acquired.wait, _LOCKED_PHASE_BOUND_S)
        await asyncio.to_thread(_join_after_ready, ("the taker", taker))
    finally:
        release.set()
        _stop_leftovers(holder, *([taker] if taker is not None else []))


def test_a_child_forked_while_a_thread_holds_the_span_takes_the_span_once_the_parent_leaves_it(tmp_path) -> None:
    """A forked child inherits no record lock and none of the parent's threads, so it starts with
    no span held: it waits only for the parent's record lock, never for a lock no thread of its own
    can release."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with mgr.exclusive_write():
            entered.set()
            release.wait(_LOCKED_PHASE_BOUND_S)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(_LOCKED_PHASE_BOUND_S), "the thread did not enter the span"
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child exits through os._exit
            try:
                os.close(read_fd)
                with FileConfigManager(config_dir_path=str(tmp_path)).exclusive_write():
                    os.write(write_fd, b"1")
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            early, _, _ = select.select([read_fd], [], [], _BLOCKED_WATCH_S)
            assert early == [], "the child entered the span while the parent held it"
            release.set()
            woke, _, _ = select.select([read_fd], [], [], _LOCKED_PHASE_BOUND_S)
            assert woke == [read_fd], "the child did not enter the span after the parent left it"
            assert os.read(read_fd, 1) == b"1"
        finally:
            os.close(read_fd)
            if os.waitpid(pid, os.WNOHANG) == (0, 0):
                os.kill(pid, 9)
                os.waitpid(pid, 0)
    finally:
        release.set()
        holder.join(_LOCKED_PHASE_BOUND_S)
