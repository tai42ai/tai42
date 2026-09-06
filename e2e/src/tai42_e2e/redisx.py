"""Harness-side Redis: reachability probe, per-stack logical-DB allocation, and
probe-record reads.

The harness owns logical isolation on a Redis server: each stack gets one logical
DB index out of the admin's ``stack_db_range``, ``FLUSHDB``-ed at allocation. On the
shared feature Redis that range is 1-15, because index 0 is the harness's own
probe-record channel. The module-capable checkpoint Redis instead gets a ONE-SLOT
range (DB 0), because RediSearch indexes live only on db 0 and their names are
server-global — see ``connect_infra``. This is a synchronous client — logical-DB
allocation happens on the boot path, not inside test coroutines."""

from __future__ import annotations

import json
import os
import threading
from typing import cast

import redis

# Index 0 carries the ``e2e:rec:*`` probe records the SUT-side ``e2e_record``
# tool writes; stacks are pinned to 1-15 so their FLUSHDB never wipes it.
_PROBE_DB = 0
_STACK_DB_RANGE = range(1, 16)


class RedisAdmin:
    """A synchronous harness Redis client for a single shared server, plus a
    round-robin logical-DB allocator.

    The shared Redis reserves logical DB 0 for the probe-record channel and
    allocates stacks over 1-15 (the default ``stack_db_range``). The checkpoint
    Redis is constructed with a one-slot range (DB 0 only): RediSearch indexes
    exist only on db 0, so it has exactly one usable slot."""

    def __init__(self, host: str, port: int, *, stack_db_range: range = _STACK_DB_RANGE) -> None:
        self._host = host
        self._port = port
        self._stack_db_range = stack_db_range
        self._lock = threading.Lock()
        self._in_use: set[int] = set()
        self._cursor = 0
        # The admin's own client (DB 0), used for the reachability/module probes
        # and the probe-record reads. On a server whose DB 0 is also allocatable
        # there is no conflict: the probes run once at session start, before any
        # stack allocates, and delete every key they write.
        self._probe = redis.Redis(host=host, port=port, db=_PROBE_DB, decode_responses=True)

    def check_reachable(self) -> None:
        """Ping the server so a missing or unreachable Redis fails loudly at
        session start, not cryptically mid-suite. A plain reachable Redis is all a
        ping confirms; the module-capable checkpoint server additionally runs
        :meth:`check_search_json_modules`."""
        self._probe.ping()

    def check_search_json_modules(self) -> None:
        """Assert the RediSearch + RedisJSON commands the langgraph redis
        checkpoint/store provider needs are available.

        A module-free Redis mistakenly pointed at the checkpoint URL pings fine but
        then fails cryptically at stack boot with ``unknown command 'FT._LIST'`` /
        ``JSON.SET``; probing both commands at session start turns that into a loud,
        early failure. ``FT._LIST`` (RediSearch) is read-only; the ``JSON.SET``
        (RedisJSON) probe writes and immediately deletes one temporary key."""
        probe_key = "tai42_e2e:module_probe"
        self._probe.execute_command("FT._LIST")
        self._probe.execute_command("JSON.SET", probe_key, "$", "1")
        self._probe.delete(probe_key)

    def allocate_db(self) -> int:
        """Reserve and FLUSHDB a logical DB index for a stack.

        Before the flush, refuse an index a leaked worker still consumes: the
        flush would wipe the orphan's keys but not stop its process, so it would
        keep dequeuing this stack's jobs / firing its parks off the shared index.
        A live foreign consumer is a loud refusal naming its pid, never a silent
        reuse (:meth:`_assert_no_live_orphan`)."""
        span = self._stack_db_range
        with self._lock:
            for _ in range(len(span)):
                idx = span[self._cursor % len(span)]
                self._cursor += 1
                if idx not in self._in_use:
                    self._assert_no_live_orphan(idx)
                    client = redis.Redis(host=self._host, port=self._port, db=idx)
                    try:
                        client.flushdb()
                    finally:
                        client.close()
                    # The slot is taken only once it is genuinely reset: a failed flush
                    # propagates without retiring the index from the pool for the run.
                    self._in_use.add(idx)
                    return idx
            raise RuntimeError(
                f"Redis logical-DB pool ({span.start}-{span.stop - 1}) exhausted; too many concurrent stacks"
            )

    def _assert_no_live_orphan(self, idx: int) -> None:
        """Refuse a logical DB that a live foreign worker still heartbeats on.

        Every SUT worker (HTTP ``serve`` and the ``backend`` consumer alike)
        refreshes a TTL-bound ``<ns>:bus:presence:<name>`` key while it runs. A
        cleanly torn-down stack leaves only expiring keys whose process is gone; a
        LEAKED worker keeps re-writing its key on this index. No stack of this run
        holds the index yet (allocation is exclusive and pre-spawn), so a presence
        key whose pid is still alive belongs to an orphan from an earlier run. The
        harness raises naming the pid rather than leasing the index under it — the
        exact cross-stack theft (a foreign backend answering this stack's tool
        jobs, a foreign reaper firing its parks) the isolation contract forbids."""
        client = redis.Redis(host=self._host, port=self._port, db=idx, decode_responses=True)
        try:
            offenders: list[str] = []
            for key in client.scan_iter(match="*:bus:presence:*"):
                value = client.get(key)
                if not isinstance(value, str):
                    continue
                try:
                    meta = json.loads(value)
                    pid = int(meta["pid"])
                except (ValueError, TypeError, KeyError):
                    continue
                if _pid_alive(pid):
                    offenders.append(f"pid {pid} (kind={meta.get('kind', '?')}, key={key!r})")
        finally:
            client.close()
        if offenders:
            raise RuntimeError(
                f"redis logical DB {idx} on {self._host}:{self._port} still carries live worker(s) "
                f"from a leaked stack: {'; '.join(offenders)}. Kill the orphan process(es) by pid "
                "before re-running — the harness refuses to lease a DB under a live foreign consumer "
                "(it would dequeue this stack's jobs and fire its parks)."
            )

    def release_db(self, idx: int) -> None:
        with self._lock:
            self._in_use.discard(idx)

    def url_for(self, idx: int) -> str:
        """The ``redis://host:port/idx`` URL feature settings point at."""
        return f"redis://{self._host}:{self._port}/{idx}"

    def records(self, key: str) -> list[str]:
        """The raw JSON strings the ``e2e_record`` probe RPUSH'd onto
        ``e2e:rec:{key}`` (logical DB 0)."""
        return cast(list[str], self._probe.lrange(f"e2e:rec:{key}", 0, -1))

    def record_keys(self) -> list[str]:
        """Every ``e2e_record`` probe key present (logical DB 0), the ``e2e:rec:``
        prefix stripped — for reading back records keyed on a value a test cannot
        know ahead of the run (a person id minted inside the SUT)."""
        prefix = "e2e:rec:"
        return [key[len(prefix) :] for key in cast(list[str], self._probe.keys(f"{prefix}*"))]

    def close(self) -> None:
        self._probe.close()


def _pid_alive(pid: int) -> bool:
    """Whether a local process id is still running. ``signal 0`` probes existence
    without delivering a signal; a permission error means the pid exists under
    another user (still alive), only ``ProcessLookupError`` means it is gone. SUT
    processes run on the harness host, so the pid a presence value carries is
    local."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
