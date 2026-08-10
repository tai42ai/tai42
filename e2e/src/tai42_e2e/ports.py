"""Ephemeral-port allocation. The skeleton does no port-availability pre-flight,
so avoiding collisions is entirely the harness's job: bind ``("127.0.0.1", 0)``,
read the port the kernel assigned, close, and record it in a process-wide
reservation set so the same port is never handed out twice within a run."""

from __future__ import annotations

import socket
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
            raise RuntimeError(f"port {port} is already in use; free it or point TAI_E2E_UI_PORT at another port")
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
