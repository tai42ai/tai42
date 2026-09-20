"""A threaded fake of the GitHub REST surfaces the ``storage-github`` plugin
speaks — the RAW read endpoint, the Contents API (base64 + blob sha), and the
recursive Git Trees listing — so the github storage-axis leg is hermetic (no real
GitHub). In-process on a fixed port of the pytest process, exactly like the other
harness net fixtures; the SUT worker processes reach it over loopback.

Single repo, one in-memory object map guarded by a lock (the leg's stacks share
one fake and key objects by per-test-unique paths). A blob sha is the sha1 of the
stored bytes — it round-trips through the Contents GET into the delete/update
payload, not matching git's real object hashing. An update or delete must carry the
current blob sha as an optimistic-concurrency precondition (a missing or stale sha is
a 409, as the real Contents API rejects a lost update), so a regression dropping the
sha fails loudly rather than staying green."""

from __future__ import annotations

import base64
import hashlib
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from tai42_e2e._threaded import ThreadedServer


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class FakeGithub:
    """The fake GitHub REST server, bound to ``host:port`` in a background thread."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._server = ThreadedServer(self._build_app(), host, port)

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def _file_payload(self, path: str, data: bytes) -> dict[str, Any]:
        # The Contents-API file object the plugin reads a ``sha`` off (for update +
        # delete) and, on a read fallback, the base64 ``content``.
        return {
            "type": "file",
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "sha": _blob_sha(data),
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/raw/{username}/{repo}/refs/heads/{branch}/{path:path}")
        async def raw(username: str, repo: str, branch: str, path: str) -> Response:
            with self._lock:
                data = self._objects.get(path)
            if data is None:
                return Response(status_code=404)
            return Response(content=data, media_type="text/plain")

        @app.get("/api/repos/{username}/{repo}/contents/{path:path}")
        async def contents_get(username: str, repo: str, path: str, ref: str | None = None) -> JSONResponse:
            with self._lock:
                data = self._objects.get(path)
            if data is None:
                return JSONResponse({"message": "Not Found"}, status_code=404)
            return JSONResponse(self._file_payload(path, data))

        @app.put("/api/repos/{username}/{repo}/contents/{path:path}")
        async def contents_put(username: str, repo: str, path: str, request: Request) -> JSONResponse:
            body = await request.json()
            data = base64.b64decode(body["content"])
            with self._lock:
                current = self._objects.get(path)
                # A PUT over an EXISTING blob carries the current blob sha as an
                # optimistic-concurrency precondition; a missing or stale sha is a 409,
                # exactly as the real Contents API rejects a lost update. A create (new
                # path) needs no sha.
                if current is not None and body.get("sha") != _blob_sha(current):
                    return JSONResponse({"message": "sha mismatch"}, status_code=409)
                self._objects[path] = data
            payload = {"content": self._file_payload(path, data), "commit": {"sha": _blob_sha(data)}}
            return JSONResponse(payload, status_code=200 if current is not None else 201)

        @app.delete("/api/repos/{username}/{repo}/contents/{path:path}")
        async def contents_delete(username: str, repo: str, path: str, request: Request) -> JSONResponse:
            body = await request.json()
            with self._lock:
                current = self._objects.get(path)
                if current is None:
                    return JSONResponse({"message": "Not Found"}, status_code=404)
                # A DELETE carries the current blob sha under the same precondition; a
                # missing or stale sha is a 409, as the real Contents API rejects a stale
                # delete.
                if body.get("sha") != _blob_sha(current):
                    return JSONResponse({"message": "sha mismatch"}, status_code=409)
                del self._objects[path]
            return JSONResponse({"commit": {"sha": "deleted"}})

        @app.get("/api/repos/{username}/{repo}/git/trees/{branch}")
        async def trees(username: str, repo: str, branch: str, recursive: str | None = None) -> JSONResponse:
            with self._lock:
                tree = [
                    {"path": p, "type": "blob", "mode": "100644", "sha": _blob_sha(self._objects[p])}
                    for p in sorted(self._objects)
                ]
            return JSONResponse({"sha": _blob_sha(b""), "truncated": False, "tree": tree})

        return app
