"""The child side of the cross-process ``FileConfigManager`` lock tests.

A ``spawn`` child imports the function it runs by its module's name. This module is
imported by its ``tests.config`` name, which resolves the same in this suite's own run
and in a whole-repo run (the suite's conftest puts the package root on ``sys.path``),
so a child finds it under either rootdir.

Both variants exercise the real ``write_env`` / ``mutate_manifest`` bodies, and both force
the overlap on events, whatever the host's scheduling. The locked variants hold their read
until the other writer is waiting on the lock (or has finished), so on every run a second
writer contends for the lock inside the first one's read-merge-write. The ``_Unlocked*``
variants neuter the flock and make both writers meet after their read: each merges against
the base it read before either wrote, so an update is lost on every round.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection

from tai42_skeleton.config.file_manager import FileConfigManager


def report_ready(ready: Connection) -> None:
    """Tell the parent this child has started and is about to do its locked work."""
    ready.send(True)
    ready.close()


class LockTraffic:
    """Both writers' lock arrivals, completions and finish, shared across the two processes."""

    def __init__(self, counts, me: int) -> None:
        self._counts = counts
        self._me = me

    def _bump(self, writer: int, field: int) -> None:
        with self._counts.get_lock():
            self._counts[3 * writer + field] += 1

    def arrived(self) -> None:
        self._bump(self._me, 0)

    def completed(self) -> None:
        self._bump(self._me, 1)

    def finished(self) -> None:
        self._bump(self._me, 2)

    def wait_until_the_other_contends(self) -> None:
        """Return once the other writer waits on the lock (arrived, not completed) or has finished."""
        other = 1 - self._me
        while True:
            with self._counts.get_lock():
                arrivals, completions, finished = self._counts[3 * other : 3 * other + 3]
            if arrivals > completions or finished:
                return
            time.sleep(0.001)


class _ContendedEnvManager(FileConfigManager):
    def __init__(self, config_dir: str, traffic: LockTraffic) -> None:
        super().__init__(config_dir)
        self._traffic = traffic

    @contextmanager
    def _file_lock(self, path: str) -> Iterator[None]:
        self._traffic.arrived()
        with super()._file_lock(path):
            yield
        self._traffic.completed()

    def read_env(self) -> dict[str, str]:
        result = super().read_env()
        self._traffic.wait_until_the_other_contends()
        return result


class _UnlockedRendezvousEnvManager(FileConfigManager):
    def __init__(self, config_dir: str, rendezvous) -> None:
        super().__init__(config_dir)
        self._rendezvous = rendezvous

    def read_env(self) -> dict[str, str]:
        result = super().read_env()
        self._rendezvous.wait()
        return result

    @contextmanager
    def _file_lock(self, path: str) -> Iterator[None]:
        yield


def env_writer(config_dir: str, prefix: str, count: int, use_lock: bool, barrier, traffic: LockTraffic, ready) -> None:
    mgr = _ContendedEnvManager(config_dir, traffic) if use_lock else _UnlockedRendezvousEnvManager(config_dir, barrier)
    report_ready(ready)
    barrier.wait()
    for i in range(count):
        mgr.write_env({f"{prefix}{i}": str(i)})
    traffic.finished()


class _ContendedPreservedManager(FileConfigManager):
    def __init__(self, config_dir: str, traffic: LockTraffic) -> None:
        super().__init__(config_dir)
        self._traffic = traffic

    @contextmanager
    def _file_lock(self, path: str) -> Iterator[None]:
        self._traffic.arrived()
        with super()._file_lock(path):
            yield
        self._traffic.completed()

    def _load_yaml_preserved(self, path: str):
        result = super()._load_yaml_preserved(path)
        if path == self.manifest_path:
            self._traffic.wait_until_the_other_contends()
        return result


class _UnlockedRendezvousPreservedManager(FileConfigManager):
    def __init__(self, config_dir: str, rendezvous) -> None:
        super().__init__(config_dir)
        self._rendezvous = rendezvous

    def _load_yaml_preserved(self, path: str):
        result = super()._load_yaml_preserved(path)
        if path == self.manifest_path:
            self._rendezvous.wait()
        return result

    @contextmanager
    def _file_lock(self, path: str) -> Iterator[None]:
        yield


def mutate_writer(config_dir: str, key: str, use_lock: bool, barrier, traffic: LockTraffic, ready) -> None:
    mgr = (
        _ContendedPreservedManager(config_dir, traffic)
        if use_lock
        else _UnlockedRendezvousPreservedManager(config_dir, barrier)
    )
    report_ready(ready)
    barrier.wait()
    mgr.mutate_manifest(lambda doc: doc.__setitem__(key, key))
    traffic.finished()


def single_env_write(config_dir: str, key: str, value: str, ready) -> None:
    mgr = FileConfigManager(config_dir_path=config_dir)
    report_ready(ready)
    mgr.write_env({key: value})
