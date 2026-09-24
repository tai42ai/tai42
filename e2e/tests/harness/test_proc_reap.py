"""Harness self-test: process teardown waits for the whole SESSION to drain.

``ProcessHandle.terminate`` SIGKILLs the master's process group and every
re-grouped survivor still in its session, but SIGKILL is asynchronous and the
harness does not parent those survivors (uvicorn worker children, an rq
work-horse) — init reaps them a beat after the kill. ``_reap_session_survivors``
must therefore BLOCK until the session is empty before returning: otherwise
teardown releases the stack's Redis DB while a worker still heartbeats its
``bus:presence`` key on it, and the next stack to lease that index trips the
live-orphan guard. A survivor that never dies raises loudly naming its pid.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tai42_e2e import procs

_SID = 55555
_WORKER = 12345


def _handle() -> procs.ProcessHandle:
    handle = procs.ProcessHandle(name="serve-1", argv=[], cwd=Path("."), env={}, log_path=Path("unused.log"))
    handle._pgid = _SID
    handle._proc = SimpleNamespace(pid=_SID)  # type: ignore[assignment]
    return handle


def _wire_session(monkeypatch: pytest.MonkeyPatch, live: set[int]) -> None:
    monkeypatch.setattr(procs, "_all_pids", lambda: sorted(live))

    def getsid(pid: int) -> int:
        if pid in live:
            return _SID
        raise ProcessLookupError

    monkeypatch.setattr(procs.os, "getsid", getsid)


def test_reap_session_survivors_waits_until_the_session_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    # The master plus one worker survivor share the session; the SIGKILL takes
    # effect (init reaps the worker), so the drain returns cleanly.
    live = {_SID, _WORKER}
    _wire_session(monkeypatch, live)
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: live.discard(pid))

    _handle()._reap_session_survivors(deadline=2.0)

    assert _WORKER not in live


def test_reap_session_survivors_raises_when_a_worker_never_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    # The survivor outlives its SIGKILL: the drain must not return silently — it
    # raises at the deadline naming the leaked pid.
    live = {_SID, _WORKER}
    _wire_session(monkeypatch, live)
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: None)

    with pytest.raises(RuntimeError, match=rf"session worker.*{_WORKER}"):
        _handle()._reap_session_survivors(deadline=0.3)
