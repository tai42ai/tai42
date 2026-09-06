"""Ephemeral-port allocation. The skeleton does no port-availability pre-flight,
so avoiding collisions is entirely the harness's job: bind ``("127.0.0.1", 0)``,
read the port the kernel assigned, close, and record it in a process-wide
reservation set so the same port is never handed out twice within a run."""

from __future__ import annotations

import socket
import subprocess
import threading

_reserved: set[int] = set()
_lock = threading.Lock()


def allocate_port() -> int:
    """Return a currently-free TCP port on loopback, reserved for this run.

    The bind-0 probe races with anything else on the host that binds after the
    probe closes, but within a single-process serial suite the reservation set
    guarantees the harness itself never double-allocates. Raises if it cannot
    find an unreserved free port in a bounded number of attempts."""
    with _lock:
        for _ in range(100):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            if port not in _reserved:
                _reserved.add(port)
                return port
        raise RuntimeError("could not allocate an unreserved free port after 100 attempts")


def reserve_specific_port(port: int) -> None:
    """Reserve a caller-chosen port (the pinned Studio app port a Playwright
    ``webServer.url`` must know up front). Raises loudly if the port is already
    reserved this run or something is already listening on it, so a collision is
    immediate and visible instead of a silent boot hang."""
    with _lock:
        if port in _reserved:
            raise RuntimeError(f"port {port} is already reserved in this run")
        if not is_free(port):
            pids = _pids_listening(port)
            who = f" (held by pid(s) {', '.join(map(str, pids))})" if pids else ""
            raise RuntimeError(
                f"port {port} is already in use{who} — likely a leaked stack from an earlier run. "
                "Kill it by pid, or point TAI_E2E_UI_PORT at another port."
            )
        _reserved.add(port)


def release_port(port: int) -> None:
    """Drop a port from the reservation set once its owner has torn down."""
    with _lock:
        _reserved.discard(port)


def is_free(port: int) -> bool:
    """True when nothing is listening on the loopback port — used by teardown to
    assert a stack really released every port it bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _pids_listening(port: int) -> list[int]:
    """Best-effort pids listening on ``port``, so a loud refusal can name the
    orphan holding it. Tries ``lsof`` then ``ss``; an absent tool or a parse miss
    yields an empty list (the refusal still fires, just without pids) rather than
    masking the collision."""
    pids = {int(tok) for tok in _tool_stdout(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"]).split() if tok.isdigit()}
    if pids:
        return sorted(pids)
    for chunk in _tool_stdout(["ss", "-ltnpH", f"sport = :{port}"]).split("pid="):
        head = chunk.split(",", 1)[0].strip()
        if head.isdigit():
            pids.add(int(head))
    return sorted(pids)


def _tool_stdout(argv: list[str]) -> str:
    """Stdout of a diagnostic tool, or "" when the tool is absent or cannot run —
    the caller's refusal still fires without pids; no error is masked into a
    false "free" verdict."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False).stdout
    except OSError:
        return ""
