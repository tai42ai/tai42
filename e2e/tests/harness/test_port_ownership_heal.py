"""Harness self-test for the boot engine's port-ownership heal.

The ephemeral-port allocator probes a free port then closes the socket before the child
binds it, so on a shared host a foreign process can seize that port in the gap; the child
then fails to bind and a busless stack's HTTP-only readiness would pass against the
foreign listener. The boot engine therefore confirms each spawned process owns the port it
was allocated: when a foreign listener holds the port the stack re-allocates and serves
from its own process, and when every bounded attempt is seized boot raises loudly naming
the port and the foreign pid.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import socket
import threading
from collections.abc import Callable
from functools import partial

import pytest

from tai42_e2e import Infra, StackConfig, StackResources, ports
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.manifests import build_minimal_stack, build_projection_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import Variants

# The real allowlist key the connector/marketplace/studio profiles fill from their app
# origin; carried on the minimal stack here purely to exercise the port-derived fill.
_ALLOWLIST_KEY = "CONNECTORS_REDIRECT_URI_ALLOWLIST"


def _minimal_with_origin_allowlist(res: StackResources, variants: Variants) -> StackConfig:
    """The smallest bootable stack, plus one ``origin_allowlist_env_keys`` entry — the
    port-derived env the heal must refresh to the re-allocated port."""
    return dataclasses.replace(build_minimal_stack(res, variants), origin_allowlist_env_keys=[_ALLOWLIST_KEY])


# The minimal stack runs one worker and no backend, so these exercise no backend seam and
# run once on the backendless leg.
pytestmark = pytest.mark.backendless


class _ForeignListener:
    """A loopback TCP listener answering a bare 401 — a stand-in for another process
    holding a port the allocator hands out."""

    def __init__(self, port: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.port = port
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                conn.close()

    def close(self) -> None:
        self._stop = True
        self._sock.close()


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _SeizeFirstAllocation:
    """Patch ``ports.allocate_port`` so the FIRST allocation (the app port) is seized by a
    foreign 401 listener; the heal's re-allocations and any later port fall through to
    free ports."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_allocate = ports.allocate_port
        self.listener: _ForeignListener | None = None

        def _allocate() -> int:
            port = real_allocate()
            if self.listener is None:
                self.listener = _ForeignListener(port)
            return port

        monkeypatch.setattr(ports, "allocate_port", _allocate)

    @property
    def port(self) -> int:
        assert self.listener is not None, "no allocation was seized"
        return self.listener.port

    def close(self) -> None:
        if self.listener is not None:
            self.listener.close()


def test_boot_reallocates_off_a_foreign_listener(
    fresh_stack: Callable[..., TaiStack], monkeypatch: pytest.MonkeyPatch
) -> None:
    seize = _SeizeFirstAllocation(monkeypatch)
    try:
        stack = fresh_stack(build_minimal_stack)
        app_port = stack.app_ports[0]
        assert app_port != seize.port, "the stack kept the seized port instead of re-allocating"
        serve = stack._procs["serve"]
        assert serve.is_running()
        # Readiness passed from OUR process: the app port is held by the serve session.
        holders = ports.listening_pids(app_port)
        assert holders, f"nothing is listening on the healed app port {app_port}"
        assert all(os.getsid(pid) == serve.pid for pid in holders), (
            f"app port {app_port} is not owned by the serve session (listeners {holders})"
        )
    finally:
        seize.close()


def test_heal_refreshes_the_origin_allowlist_to_the_new_port(
    fresh_stack: Callable[..., TaiStack], monkeypatch: pytest.MonkeyPatch
) -> None:
    seize = _SeizeFirstAllocation(monkeypatch)
    try:
        stack = fresh_stack(_minimal_with_origin_allowlist)
        app_port = stack.app_ports[0]
        assert app_port != seize.port, "the stack kept the seized port instead of re-allocating"
        new_origin = f"http://{stack.host}:{app_port}"
        seized_origin = f"http://{stack.host}:{seize.port}"
        # The serve process was (re)spawned advertising its NEW origin, never the seized one.
        assert stack._specs["serve"].env[_ALLOWLIST_KEY] == new_origin, stack._specs["serve"].env[_ALLOWLIST_KEY]
        # The reload path reads .env, which must carry the same refreshed origin.
        env_text = (stack._config_dir / ".env").read_text()
        assert f"{_ALLOWLIST_KEY}={new_origin}" in env_text, env_text
        assert seized_origin not in env_text, env_text
    finally:
        seize.close()


def test_non_bind_early_exit_propagates_unchanged(
    fresh_stack: Callable[..., TaiStack], caplog: pytest.LogCaptureFixture
) -> None:
    # A serve that exits for its OWN reason (an api_tools.include naming an unregistered
    # op) is NOT a port collision: the original readiness error carrying the child's
    # failure must propagate untouched, with no re-allocation and no heal warning.
    with (
        caplog.at_level(logging.WARNING, logger="tai42_e2e.stack"),
        pytest.raises(RuntimeError, match="totally_unknown_op"),
    ):
        fresh_stack(partial(build_projection_stack, api_tools={"enabled": True, "include": ["totally_unknown_op"]}))
    assert "did not own its port" not in caplog.text, caplog.text


def test_boot_raises_when_every_allocation_is_seized(
    infra: Infra, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = _ForeignListener(_free_port())
    # Every allocation — the first and each heal attempt — lands on the seized port, so the
    # bounded attempts are spent and boot must raise.
    monkeypatch.setattr(ports, "allocate_port", lambda: listener.port)
    root = tmp_path_factory.mktemp("heal-exhaust")
    resources, config = allocate_and_build(infra, root, build_minimal_stack, None, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            stack.boot()
        message = str(excinfo.value)
        assert f"port {listener.port}" in message, message
        assert f"foreign pid(s) {os.getpid()}" in message, message
        assert "could not bind an owned port" in message, message
    finally:
        listener.close()
        stack.teardown()
