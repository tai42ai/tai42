"""The resident memory of a serve's process tree, read from one process-table snapshot.

One ``ps -axo pid=,ppid=,rss=,nlwp=`` read (procps) gives every live process with its parent,
resident set (KiB) and thread count at one moment, so a process that exits while the tree is
read — a compute-pool child being replaced — is simply absent rather than an error."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessMemory:
    pid: int
    ppid: int
    rss_kb: int
    threads: int


class ProcessTableError(RuntimeError):
    """The process table could not be read as pid/ppid/rss/threads rows, or does not hold the serve."""


def process_table() -> dict[int, ProcessMemory]:
    """Every live process with its parent, resident set and thread count, from one
    ``ps -axo pid=,ppid=,rss=,nlwp=`` read.

    A failed ``ps`` raises :class:`subprocess.CalledProcessError`; a row that is not four
    integers, or a table with no row, raises :class:`ProcessTableError`."""
    listing = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,nlwp="], capture_output=True, text=True, check=True)
    table: dict[int, ProcessMemory] = {}
    for line in listing.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 4:
            raise ProcessTableError(f"process table row is not pid/ppid/rss/threads: {line!r}")
        try:
            pid, ppid, rss_kb, threads = (int(field) for field in fields)
        except ValueError as exc:
            raise ProcessTableError(f"process table row is not pid/ppid/rss/threads: {line!r}") from exc
        table[pid] = ProcessMemory(pid=pid, ppid=ppid, rss_kb=rss_kb, threads=threads)
    if not table:
        raise ProcessTableError("the process table listed no process")
    return table


@dataclass(frozen=True)
class ServeMemory:
    master: ProcessMemory
    # Depth 1 under the master: the uvicorn workers.
    workers: tuple[ProcessMemory, ...]
    # Depth 2: each worker's compute-pool children (and a worker's multiprocessing helper,
    # held to the same bound).
    children: tuple[ProcessMemory, ...]
    # The master plus every descendant at any depth.
    total_kb: int


def serve_memory(root_pid: int, table: Mapping[int, ProcessMemory] | None = None) -> ServeMemory:
    """Classify the tree under ``root_pid`` by depth; raise :class:`ProcessTableError` when the root is absent.

    ``table`` defaults to a fresh :func:`process_table` read."""
    rows = process_table() if table is None else table
    master = rows.get(root_pid)
    if master is None:
        raise ProcessTableError(f"the serve process {root_pid} is not in the process table")
    offspring: dict[int, list[ProcessMemory]] = {}
    for row in rows.values():
        if row.pid != row.ppid:
            offspring.setdefault(row.ppid, []).append(row)
    workers: list[ProcessMemory] = []
    children: list[ProcessMemory] = []
    total_kb = master.rss_kb
    level = [master]
    depth = 0
    while level:
        depth += 1
        level = sorted((row for parent in level for row in offspring.get(parent.pid, [])), key=lambda row: row.pid)
        total_kb += sum(row.rss_kb for row in level)
        if depth == 1:
            workers = level
        elif depth == 2:
            children = level
    return ServeMemory(master=master, workers=tuple(workers), children=tuple(children), total_kb=total_kb)
