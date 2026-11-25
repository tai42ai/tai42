"""Harness self-test: the serve's process tree is read from one process table and classified by depth.

``serve_memory`` takes the serve as the root, its children as the uvicorn workers, their
children as the compute-pool children, and totals the root with every descendant; a process
outside the tree counts nowhere. Every row carries the process's thread count. A table it
cannot read, and a serve that is not in it, raise."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from tai42_e2e import rss

pytestmark = pytest.mark.needs("no-stack")

_ROOT = 100
_TABLE = """\
      1       0   9000    1
    100       1  50000    3
    201     100  80000   19
    202     100  81000   47
    301     201  60000    1
    302     201  61000    1
    303     201   2000    1
    311     202  62000    1
    312     202  63000    1
    401       1  99999    2
    402     401  12345    1
"""


def _wire_ps(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs.get("check") is True, "the process table read must fail on a non-zero ps exit"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(rss.subprocess, "run", run)
    return calls


def test_serve_memory_classifies_the_tree_by_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_ps(monkeypatch, _TABLE)

    memory = rss.serve_memory(_ROOT)

    assert calls == [["ps", "-axo", "pid=,ppid=,rss=,nlwp="]]
    assert memory.master == rss.ProcessMemory(pid=100, ppid=1, rss_kb=50000, threads=3)
    assert [(worker.pid, worker.threads) for worker in memory.workers] == [(201, 19), (202, 47)]
    assert [child.pid for child in memory.children] == [301, 302, 303, 311, 312]
    # The root and every descendant; neither init nor the unrelated tree under 401.
    assert memory.total_kb == 50000 + 80000 + 81000 + 60000 + 61000 + 2000 + 62000 + 63000


def test_serve_memory_counts_descendants_below_the_children_in_the_total() -> None:
    table = {
        100: rss.ProcessMemory(pid=100, ppid=1, rss_kb=10, threads=1),
        200: rss.ProcessMemory(pid=200, ppid=100, rss_kb=20, threads=1),
        300: rss.ProcessMemory(pid=300, ppid=200, rss_kb=30, threads=1),
        400: rss.ProcessMemory(pid=400, ppid=300, rss_kb=40, threads=1),
    }

    memory = rss.serve_memory(100, table)

    assert [child.pid for child in memory.children] == [300]
    assert memory.total_kb == 100


def test_a_malformed_row_raises_naming_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_ps(monkeypatch, "    100       1  50000    3\n    201     100  not-a-number   19\n")

    with pytest.raises(rss.ProcessTableError, match="not-a-number"):
        rss.serve_memory(_ROOT)


def test_a_row_without_four_fields_raises_naming_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_ps(monkeypatch, "    100       1  50000    3\n    201     100  80000\n")

    with pytest.raises(rss.ProcessTableError, match="201     100  80000"):
        rss.serve_memory(_ROOT)


def test_an_empty_table_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_ps(monkeypatch, "\n")

    with pytest.raises(rss.ProcessTableError, match="no process"):
        rss.process_table()


def test_a_serve_absent_from_the_table_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_ps(monkeypatch, _TABLE)

    with pytest.raises(rss.ProcessTableError, match="12345"):
        rss.serve_memory(12345)


def test_a_failed_ps_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv, stderr="ps: failed")

    monkeypatch.setattr(rss.subprocess, "run", run)

    with pytest.raises(subprocess.CalledProcessError):
        rss.serve_memory(_ROOT)
