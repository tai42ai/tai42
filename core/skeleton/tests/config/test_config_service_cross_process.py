"""Two worker processes writing through ``ConfigService`` over one shared file config store.

A settings-profile apply in one worker drops a stored key while, during its build, a second
worker writes a manifest marker on that key. The second write waits for the profile's write
span to end, then validates against the env the store now converges every worker to, so it is
refused naming the key, and the store never holds a marker no worker can resolve.

The wait is observed, not inferred: the profile's build runs on until the kernel's lock table
(``/proc/locks``) shows the second worker blocked on the span's lock file while the profile
worker holds it, before the profile's env has persisted.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import os
import time
from pathlib import Path

import pytest
from tests.config._config_service_children import (
    EVENT_BOUND_S,
    marker_on_the_dropped_key,
    profile_dropping_a_key,
    shared_manifest,
)

from tai42_skeleton.config.file_manager import FileConfigManager

from .fake_pipeline import _reset_settings_after  # noqa: F401  — autouse fixture, active on import

# A spawned child's interpreter start-up plus its write, on a loaded host.
_CHILD_BOUND_S = 240.0


def _span_lock_table(lock_file: Path) -> tuple[set[int], set[int]]:
    """The pids holding and the pids blocked waiting on a POSIX record lock of *lock_file*.

    Read from ``/proc/locks``, where a lock reads ``N: POSIX ADVISORY WRITE <pid> <maj>:<min>:<inode> ...``
    and a blocked request carries ``->`` after ``N:``.
    """
    st = os.stat(lock_file)
    subject = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    holders: set[int] = set()
    waiters: set[int] = set()
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()[1:]
        blocked = bool(fields) and fields[0] == "->"
        fields = fields[1:] if blocked else fields
        if len(fields) >= 5 and fields[0] == "POSIX" and fields[4] == subject:
            (waiters if blocked else holders).add(int(fields[3]))
    return holders, waiters


def _await_marker_waiting_on_the_profile_span(
    lock_file: Path,
    profile_pid: int,
    marker_pid: int,
    marker_out: multiprocessing.connection.Connection,
    persisted,
    deadline: float,
) -> None:
    """Return once the marker worker is blocked on the span the profile worker holds, before the profile persisted."""
    while True:
        holders, waiters = _span_lock_table(lock_file) if lock_file.exists() else (set(), set())
        if holders == {profile_pid} and marker_pid in waiters:
            assert not persisted.is_set(), "the profile persisted before the marker write was seen waiting"
            return
        if marker_out.poll():
            pytest.fail(f"the marker write ended without waiting on the write span: {marker_out.recv()}")
        if time.monotonic() > deadline:
            pytest.fail(
                f"the marker worker was never seen blocked on the write span the profile worker held "
                f"(last seen: holders {sorted(holders)}, waiters {sorted(waiters)})"
            )
        time.sleep(0.01)


def _outcome(reader: multiprocessing.connection.Connection, name: str, deadline: float) -> str:
    if not reader.poll(max(0.0, deadline - time.monotonic())):
        pytest.fail(f"{name} reported nothing within {_CHILD_BOUND_S} s")
    try:
        return reader.recv()
    except EOFError:
        pytest.fail(f"{name} exited before it reported how its write ended")


def test_a_marker_written_by_one_worker_during_another_workers_profile_apply_is_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = str(tmp_path)
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", config_dir)
    manager = FileConfigManager(config_dir_path=config_dir)
    manager.replace_manifest(shared_manifest())
    manager.replace_env({"M_PICK": "m_a", "M_GONE": "1"})

    ctx = multiprocessing.get_context("spawn")
    in_build, other_writing, persisted, marker_waiting = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Event()
    profile_out, profile_in = ctx.Pipe(duplex=False)
    marker_out, marker_in = ctx.Pipe(duplex=False)
    events = (in_build, other_writing, persisted)
    profile = ctx.Process(target=profile_dropping_a_key, args=(config_dir, *events, marker_waiting, profile_in))
    marker = ctx.Process(target=marker_on_the_dropped_key, args=(config_dir, *events, marker_in))
    profile.start()
    marker.start()
    # Only the children keep the write ends, so a child exiting without a report reads as end-of-file.
    profile_in.close()
    marker_in.close()
    try:
        deadline = time.monotonic() + _CHILD_BOUND_S
        assert profile.pid is not None
        assert marker.pid is not None
        _await_marker_waiting_on_the_profile_span(
            tmp_path / ".config-write.lock", profile.pid, marker.pid, marker_out, persisted, deadline
        )
        marker_waiting.set()
        profile_outcome = _outcome(profile_out, "the profile worker", deadline)
        marker_outcome = _outcome(marker_out, "the marker worker", deadline)
        profile.join(EVENT_BOUND_S)
        marker.join(EVENT_BOUND_S)
        assert (profile.exitcode, marker.exitcode) == (0, 0)
    finally:
        for proc in (profile, marker):
            if proc.is_alive():
                proc.kill()
                proc.join()

    assert profile_outcome == "persisted"
    assert marker_outcome.startswith("refused (profile persisted first: True)"), marker_outcome
    assert "markers dangling" in marker_outcome
    assert "M_GONE" in marker_outcome
    assert manager.read_env() == {"M_PICK": "m_b"}
    stored = manager.read_manifest_preserved()
    assert "headers" not in stored["mcp"][0]["config"]
    # A fresh worker reads the stored manifest under the stored env and its system env.
    monkeypatch.delenv("M_GONE", raising=False)
    monkeypatch.setenv("M_PICK", "m_b")
    monkeypatch.setenv("M_SYSTEM", "m_c")
    assert "N/A" not in str(manager.read_manifest())
