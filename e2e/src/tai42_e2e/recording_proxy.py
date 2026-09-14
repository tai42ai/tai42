"""Thread-hosted observation servers: an HTTP target the ``e2e_http_probe`` tool
GETs (recording every request) and a recording HTTP CONNECT proxy. Neither is a
compose service or a SUT process."""

from __future__ import annotations

import contextlib
import select
import socket
import socketserver
import threading
import time
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port
from tai42_e2e.waiting import wait_for


@dataclass
class RequestRecord:
    path: str
    headers: dict[str, str]


class TargetServer:
    """An HTTP server the ``e2e_http_probe`` tool GETs. Records every request so
    a test can assert the target was actually hit (and only once)."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.records: list[RequestRecord] = []
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/ok")
        async def ok(request: Request) -> PlainTextResponse:
            self.records.append(RequestRecord(path="/ok", headers=dict(request.headers)))
            return PlainTextResponse("target-ok")

        @app.get("/doc.txt")
        async def doc(request: Request, body: str = "target-document") -> PlainTextResponse:
            # A text document the file_loader tool fetches and extracts. The ``.txt``
            # suffix gives the loader a type hint, and ``body`` lets a caller inject a
            # unique marker so the extracted text is asserted against what was served.
            self.records.append(RequestRecord(path="/doc.txt", headers=dict(request.headers)))
            return PlainTextResponse(body)

        @app.get("/slow")
        async def slow(request: Request, ms: int = 0) -> PlainTextResponse:
            self.records.append(RequestRecord(path="/slow", headers=dict(request.headers)))
            # A bounded server-side delay for the interleaving test; not a client
            # wait, so it is not the banned kind of sleep.
            time.sleep(ms / 1000.0)  # noqa: TID251 — server-side response latency, not a client poll
            return PlainTextResponse("target-slow")

        return app


class _ConnectHandler(socketserver.BaseRequestHandler):
    """Handle one HTTP CONNECT tunnel: parse the target, record it, dial the
    target, answer 200, then relay bytes both ways until either side closes."""

    def handle(self) -> None:
        client = self.request
        header = self._read_headers(client)
        if not header:
            return
        request_line = header.decode("latin-1").split("\r\n", 1)[0]
        parts = request_line.split(" ")
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        target = parts[1]
        host, _, port_str = target.partition(":")
        port = int(port_str or "80")
        self.server.records.append(target)  # type: ignore[attr-defined]
        try:
            upstream = socket.create_connection((host, port), timeout=10.0)
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self._relay(client, upstream)

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> None:
        socks = [a, b]
        try:
            while True:
                readable, _, errored = select.select(socks, [], socks, 30.0)
                if errored or not readable:
                    break
                for src in readable:
                    dst = b if src is a else a
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
        finally:
            for sock in socks:
                with contextlib.suppress(OSError):
                    sock.close()


class _ThreadingProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int]) -> None:
        super().__init__(addr, _ConnectHandler)
        self.records: list[str] = []


class RecordingConnectProxy:
    """A minimal HTTP CONNECT proxy that records each CONNECT target. "Went
    through the proxy" == an entry in :attr:`records`."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self._server = _ThreadingProxyServer((host, self.port))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def records(self) -> list[str]:
        return list(self._server.records)

    def start(self) -> None:
        self._thread.start()
        wait_for(
            lambda: self._server.socket.fileno() != -1,
            deadline=5.0,
            message="CONNECT proxy never bound",
        )

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
