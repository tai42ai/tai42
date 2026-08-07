"""FileConfigManager read/write coverage over a faked filesystem (``tmp_path``).

Exercises the path resolution (constructor arg / ``TAI_CONFIG_DIR_PATH`` / ``/app``
default and the ``TAI_MANIFEST_PATH`` override), the ``.env`` read/write merge
(preserve + drop-empty), and the manifest read plus the ``mutate_manifest`` /
``replace_manifest`` write seams (``!ENV`` expansion vs preservation,
missing-file behavior, defaults backfill)."""

from __future__ import annotations

import multiprocessing
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import yaml

import tai42_skeleton.config.file_manager as fm
from tai42_skeleton.config.file_manager import FileConfigManager

# --- cross-process lock helpers (module level so ``spawn`` can import them) --
#
# The RMW window (re-read current → merge → atomic write) is widened by a small
# sleep so two processes race deterministically; the ``_Unlocked*`` variants
# disable the flock to prove an update is lost WITHOUT it. Both variants exercise
# the real ``write_env`` / ``mutate_manifest`` bodies — only the read is slowed and
# (for the unlocked variants) the lock is neutered.

_LOCK_RACE_DELAY = 0.05


class _SlowEnvManager(FileConfigManager):
    def read_env(self) -> dict[str, str]:
        result = super().read_env()
        time.sleep(_LOCK_RACE_DELAY)
        return result


class _UnlockedSlowEnvManager(_SlowEnvManager):
    @contextmanager
    def _file_lock(self, path: str) -> Iterator[None]:
        yield


def _env_writer(config_dir: str, prefix: str, count: int, use_lock: bool, barrier) -> None:
    mgr = _SlowEnvManager(config_dir) if use_lock else _UnlockedSlowEnvManager(config_dir)
    barrier.wait()
    for i in range(count):
        mgr.write_env({f"{prefix}{i}": str(i)})


class _SlowPreservedManager(FileConfigManager):
    def _load_yaml_preserved(self, path: str):
        result = super()._load_yaml_preserved(path)
        time.sleep(_LOCK_RACE_DELAY)
        return result


class _UnlockedSlowPreservedManager(_SlowPreservedManager):
    @contextmanager
    def _file_lock(self, path: str) -> Iterator[None]:
        yield


def _mutate_writer(config_dir: str, key: str, use_lock: bool, barrier) -> None:
    mgr = _SlowPreservedManager(config_dir) if use_lock else _UnlockedSlowPreservedManager(config_dir)
    barrier.wait()
    mgr.mutate_manifest(lambda doc: doc.__setitem__(key, key))


def _single_env_write(config_dir: str, key: str, value: str) -> None:
    FileConfigManager(config_dir_path=config_dir).write_env({key: value})


def _run_two_writers(target, args_a: tuple, args_b: tuple) -> None:
    """Run ``target`` in two spawned processes, released together by a barrier."""
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    proc_a = ctx.Process(target=target, args=(*args_a, barrier))
    proc_b = ctx.Process(target=target, args=(*args_b, barrier))
    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=60)
    proc_b.join(timeout=60)
    assert proc_a.exitcode == 0, "writer A did not exit cleanly"
    assert proc_b.exitcode == 0, "writer B did not exit cleanly"


# --- path resolution --------------------------------------------------------


def test_default_dir_is_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_CONFIG_DIR_PATH", raising=False)
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    mgr = FileConfigManager()
    assert mgr._env_path == os.path.join("/app", ".env")
    assert mgr._manifest_path == os.path.join("/app", "manifest.yml")
    assert mgr._defaults_manifest_path == os.path.join("/app", "templates", "manifest.yml")


def test_config_dir_path_env_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(tmp_path))
    mgr = FileConfigManager()
    assert mgr._env_path == os.path.join(str(tmp_path), ".env")


def test_constructor_arg_wins(tmp_path) -> None:
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr._env_path.startswith(str(tmp_path))


def test_tai_manifest_path_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    override = str(tmp_path / "custom.yml")
    monkeypatch.setenv("TAI_MANIFEST_PATH", override)
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    assert mgr._manifest_path == override


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


def test_write_env_rejects_value_that_cannot_round_trip(tmp_path) -> None:
    """A value ending in a single backslash cannot be represented as a
    double-quoted ``.env`` literal that the parser reads back — ``write_env``
    raises ``ValueError`` naming the key and leaves any existing store untouched
    instead of silently dropping that key (and every key after it) on reload."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    # Pre-existing store that must survive the rejected write.
    mgr.write_env({"KEEP": "safe"})
    before = (tmp_path / ".env").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match=r"WINPATH.*cannot be round-tripped"):
        mgr.write_env({"WINPATH": "C:\\data\\"})

    # The write did not happen: the store is byte-for-byte unchanged.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == before
    assert mgr.read_env() == {"KEEP": "safe"}


def test_write_env_rejects_value_that_cannot_round_trip_leaves_no_file(tmp_path) -> None:
    """When there is no pre-existing store, a value that cannot round-trip raises
    and no ``.env`` file is created."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(ValueError, match="cannot be round-tripped"):
        mgr.write_env({"WINPATH": "C:\\data\\"})
    assert not (tmp_path / ".env").exists()


def test_write_env_rejects_bad_value_in_last_position_via_sentinel(tmp_path) -> None:
    """A value that cannot round-trip is caught even when it is the LAST key written
    (its trailing backslash would otherwise pair with the closing quote only to be
    exposed by the next appended key). The trailing sentinel binding exercises that
    last position now, so the write raises whether the bad value is last or not."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    # Bad value in the LAST position: only the appended sentinel sits after it, so
    # the sentinel is what surfaces the fault.
    with pytest.raises(ValueError, match=r"WINPATH.*cannot be round-tripped"):
        mgr.write_env({"GOOD": "ok", "WINPATH": "C:\\data\\"})
    assert not (tmp_path / ".env").exists()

    # Same bad value NOT last (a good key follows it) also raises.
    with pytest.raises(ValueError, match=r"WINPATH.*cannot be round-tripped"):
        mgr.write_env({"WINPATH": "C:\\data\\", "GOOD": "ok"})
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


def test_replace_env_rejects_value_that_cannot_round_trip_leaves_no_file(tmp_path) -> None:
    """A value the parser cannot round-trip fails loudly and writes nothing, so the
    whole-map replace never lands a corrupt store."""
    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    with pytest.raises(ValueError, match="cannot be round-tripped"):
        mgr.replace_env({"WINPATH": "C:\\data\\"})
    assert not (tmp_path / ".env").exists()


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
    dir. Under the flock the RMWs serialize, so every key from both survives."""
    (tmp_path / ".env").write_text("", encoding="utf-8")  # exists so read_env re-reads
    _run_two_writers(_env_writer, (str(tmp_path), "a", 5, True), (str(tmp_path), "b", 5, True))
    result = FileConfigManager(config_dir_path=str(tmp_path)).read_env()
    expected = {f"a{i}": str(i) for i in range(5)} | {f"b{i}": str(i) for i in range(5)}
    assert result == expected


def test_write_env_concurrent_processes_lose_update_without_lock(tmp_path) -> None:
    """The SAME race without the flock loses at least one update — proving the race is
    real and the lock in the companion test is what saves it."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    _run_two_writers(_env_writer, (str(tmp_path), "a", 5, False), (str(tmp_path), "b", 5, False))
    result = FileConfigManager(config_dir_path=str(tmp_path)).read_env()
    expected = {f"a{i}": str(i) for i in range(5)} | {f"b{i}": str(i) for i in range(5)}
    assert result != expected


def test_mutate_manifest_concurrent_processes_keep_both_edits_with_lock(tmp_path) -> None:
    """Two spawned processes each ``mutate_manifest`` a disjoint key. The widened
    transaction lock spans each read → mutate → write, so both edits land."""
    (tmp_path / "manifest.yml").write_text("seed: 0\n", encoding="utf-8")
    _run_two_writers(_mutate_writer, (str(tmp_path), "alpha", True), (str(tmp_path), "beta", True))
    result = FileConfigManager(config_dir_path=str(tmp_path)).read_manifest()
    assert result["alpha"] == "alpha"
    assert result["beta"] == "beta"


def test_mutate_manifest_concurrent_processes_lose_update_without_lock(tmp_path) -> None:
    """The SAME mutate race without the lock loses one edit — proving the widened
    transaction span is what fixes the lost update the seam exists to close."""
    (tmp_path / "manifest.yml").write_text("seed: 0\n", encoding="utf-8")
    _run_two_writers(_mutate_writer, (str(tmp_path), "alpha", False), (str(tmp_path), "beta", False))
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
    fd = os.open(f"{mgr._env_path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_single_env_write, args=(str(tmp_path), "A", "1"))
    child.start()
    try:
        child.join(timeout=2.0)
        # While the sidecar lock is held the child cannot acquire it — it is still
        # running and has written nothing.
        assert child.is_alive()
        assert not (tmp_path / ".env").exists()
        # Release the lock; the child now acquires it and completes the write.
        fcntl.flock(fd, fcntl.LOCK_UN)
        child.join(timeout=30)
        assert child.exitcode == 0
        assert FileConfigManager(config_dir_path=str(tmp_path)).read_env() == {"A": "1"}
    finally:
        if child.is_alive():
            child.terminate()
            child.join()
        os.close(fd)


def test_write_env_locks_the_sidecar_not_the_target(tmp_path) -> None:
    """Holding a lock on the TARGET ``.env`` file itself does NOT block a child's
    ``write_env`` — the manager locks the sidecar, never the target (a lock on the
    target would be swapped out from under by ``os.replace`` anyway)."""
    import fcntl

    mgr = FileConfigManager(config_dir_path=str(tmp_path))
    mgr.write_env({"SEED": "0"})  # create the target so it can be locked
    fd = os.open(mgr._env_path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_single_env_write, args=(str(tmp_path), "A", "1"))
    child.start()
    try:
        child.join(timeout=30)
        # The child locked the SIDECAR (which we do not hold), so it completed despite
        # our lock on the target.
        assert child.exitcode == 0
        assert FileConfigManager(config_dir_path=str(tmp_path)).read_env()["A"] == "1"
    finally:
        if child.is_alive():
            child.terminate()
            child.join()
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
