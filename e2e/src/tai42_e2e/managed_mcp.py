"""The managed FastMCP server run as a killable streamable-http subprocess a test owns."""

from __future__ import annotations

import contextlib
import functools
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from tai42_e2e import diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.manifests import build_replicas_mcp_stack
from tai42_e2e.ports import allocate_port
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

if TYPE_CHECKING:
    from tai42_e2e.topology import Infra


class ManagedHttpMcpServer:
    """The managed FastMCP server run over streamable-http on a fixed loopback port
    the test owns, so it can be SIGKILLed and restarted between tool calls. The port
    is allocated once and reused across restarts so the manifest url stays valid."""

    def __init__(self, log_dir: Path) -> None:
        self.host = "127.0.0.1"
        self.port = allocate_port()
        self._log_dir = log_dir
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_file = None
        self._launch_index = 0

    @property
    def url(self) -> str:
        """The streamable-http MCP endpoint the manifest entry binds to."""
        return f"http://{self.host}:{self.port}/mcp"

    def start(self) -> None:
        """Launch the server as its own session leader (so the whole group is
        signalable) with the harness interpreter — which already imports
        ``tai42_e2e_fixtures`` and fastmcp — no network, no package index, no ``uvx``."""
        if self._proc is not None:
            raise RuntimeError("managed http MCP server is already running")
        self._launch_index += 1
        log_path = self._log_dir / f"managed-mcp-http-{self._launch_index}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = log_path.open("wb")
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tai42_e2e_fixtures.managed_mcp_server",
                "--transport",
                "http",
                "--host",
                self.host,
                "--port",
                str(self.port),
            ],
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def kill(self) -> None:
        """SIGKILL the server and reap it — an abrupt teardown that leaves the app's
        pooled session facing a genuinely dead connection, never a graceful close."""
        proc = self._require_running()
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=10)
        self._proc = None
        self._close_log()

    def restart(self) -> None:
        self.kill()
        self.start()

    def stop(self) -> None:
        """Teardown: SIGKILL if still running, then close the log handle. Idempotent —
        a scenario that leaves the server dead (state c) tears down cleanly."""
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            self._proc.wait(timeout=10)
            self._proc = None
        self._close_log()

    async def wait_serving(self, *, deadline: float = 20.0) -> None:
        """Poll until the http endpoint answers at all (any HTTP status proves the
        server is up and the ``/mcp`` app has mounted); a transport error means not
        listening yet, so keep polling."""

        async def up() -> bool | None:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.get(self.url)
            except httpx.TransportError:
                return None
            return True

        await wait_for_async(up, deadline=deadline, message=f"managed http MCP server at {self.url} never came up")

    def _require_running(self) -> subprocess.Popen[bytes]:
        if self._proc is None:
            raise RuntimeError("managed http MCP server is not running")
        return self._proc

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


@contextlib.contextmanager
def replicas_mcp_stack(infra: Infra, root: Path, server: ManagedHttpMcpServer) -> Iterator[TaiStack]:
    """Boot the REPLICAS pair with ``server`` mounted as the ``acme`` MCP entry on both.

    The server's liveness at boot is the caller's: a server already serving binds its tools
    on both replicas, one not yet started leaves ``acme`` on each replica's failed list. The
    stack is torn down on exit; the server is not (the caller owns it)."""
    builder = functools.partial(build_replicas_mcp_stack, mcp_url=server.url)
    resources, config = allocate_and_build(infra, root, builder, None, False)
    stack = TaiStack(config, infra, resources, root)
    with stack, diagnostics.track(stack):
        yield stack
