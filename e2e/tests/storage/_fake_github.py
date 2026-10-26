"""A threaded fake of the GitHub surfaces the ``storage-github`` plugin speaks —
the Contents API (object, raw and default forms; base64 writes keyed by blob sha),
the Git refs API for the branch head, the raw host at a commit (with ``Range``), and
the recursive Git Trees listing (at the branch or at ``branch:directory``) — so the
github storage-axis leg is hermetic (no real GitHub). In-process on a fixed port of
the pytest process, exactly like the other harness net fixtures; the SUT worker
processes reach it over loopback.

Single repo and branch, one in-memory object map guarded by a lock (the leg's
stacks share one fake and key objects by per-test-unique paths). A blob sha is the
sha1 of the stored bytes — it round-trips through the Contents GET into the
delete/update payload, not matching git's real object hashing. An update or delete
must carry the current blob sha as an optimistic-concurrency precondition (a missing
or stale sha is a 409, as the real Contents API rejects a lost update), so a
regression dropping the sha fails loudly rather than staying green.

The branch head is a commit sha that changes on every successful write and delete,
and the raw host serves only the head commit: a ranged read made at an older commit
is a 404, so a reader that does not resolve the head afresh fails loudly. The
Contents API answers only at the configured branch (``ref``)."""

from __future__ import annotations

import base64
import hashlib
import re
import threading
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from tai42_e2e._threaded import ThreadedServer

_OBJECT_FORM = "application/vnd.github.object+json"
_RAW_FORM = "application/vnd.github.raw+json"
_RANGE = re.compile(r"bytes=(\d+)-(\d+)")


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _commit_sha(serial: int) -> str:
    return hashlib.sha1(f"commit {serial}".encode()).hexdigest()


class FakeGithub:
    """The fake GitHub server for one ``branch``, bound to ``host:port`` in a background thread."""

    def __init__(self, host: str, port: int, branch: str) -> None:
        self.host = host
        self.port = port
        self.branch = branch
        self._objects: dict[str, bytes] = {}
        self._commits = 0
        self._lock = threading.Lock()
        self._server = ThreadedServer(self._build_app(), host, port)

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def _head(self) -> str:
        return _commit_sha(self._commits)

    def _commit(self) -> str:
        # Called under the lock by every successful write and delete.
        self._commits += 1
        return self._head()

    def _children(self, path: str) -> list[str]:
        prefix = path.strip("/") + "/"
        return sorted({p[len(prefix) :].split("/", 1)[0] for p in self._objects if p.startswith(prefix)})

    def _file_payload(self, path: str, data: bytes) -> dict[str, Any]:
        # The Contents-API file object: ``sha`` for update + delete, ``size`` for stat.
        return {
            "type": "file",
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "sha": _blob_sha(data),
            "size": len(data),
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }

    def _entries(self, path: str, names: list[str]) -> list[dict[str, Any]]:
        base = path.strip("/")
        entries: list[dict[str, Any]] = []
        for name in names:
            child = f"{base}/{name}"
            data = self._objects.get(child)
            kind = "dir" if data is None else "file"
            entries.append({"type": kind, "name": name, "path": child, "size": 0 if data is None else len(data)})
        return entries

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/api/repos/{username}/{repo}/git/ref/heads/{branch:path}")
        async def ref(username: str, repo: str, branch: str) -> JSONResponse:
            if branch != self.branch:
                return JSONResponse({"message": "Not Found"}, status_code=404)
            with self._lock:
                head = self._head()
            return JSONResponse({"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": head}})

        @app.get("/raw/{username}/{repo}/{commit}/{path:path}")
        async def raw(username: str, repo: str, commit: str, path: str, request: Request) -> Response:
            with self._lock:
                data = self._objects.get(path) if commit == self._head() else None
            if data is None:
                return Response(status_code=404)
            asked = request.headers.get("range")
            if asked is None:
                return Response(content=data, media_type="text/plain")
            match = _RANGE.fullmatch(asked)
            if match is None:
                return Response(status_code=400)
            start, end = int(match.group(1)), int(match.group(2))
            size = len(data)
            if start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            return Response(
                content=data[start : end + 1],
                status_code=206,
                media_type="text/plain",
                headers={"Content-Range": f"bytes {start}-{end}/{size}"},
            )

        @app.get("/api/repos/{username}/{repo}/contents/{path:path}")
        async def contents_get(
            username: str, repo: str, path: str, request: Request, ref: str | None = None
        ) -> Response:
            if ref is not None and ref != self.branch:
                return JSONResponse({"message": "No commit found for the ref"}, status_code=404)
            with self._lock:
                data = self._objects.get(path)
                children = self._children(path) if data is None else []
                entries = self._entries(path, children)
            if data is None and not entries:
                return JSONResponse({"message": "Not Found"}, status_code=404)
            accept = request.headers.get("accept", "")
            if data is None:
                if accept == _OBJECT_FORM:
                    return JSONResponse(
                        {"type": "dir", "name": path.rsplit("/", 1)[-1], "path": path, "entries": entries}
                    )
                return JSONResponse(entries)
            if accept == _RAW_FORM:
                return Response(content=data, media_type=f"{_RAW_FORM}; charset=utf-8")
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
                head = self._commit()
            payload = {"content": self._file_payload(path, data), "commit": {"sha": head}}
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
                head = self._commit()
            return JSONResponse({"commit": {"sha": head}})

        @app.get("/api/repos/{username}/{repo}/git/trees/{tree_ish:path}")
        async def trees(username: str, repo: str, tree_ish: str, recursive: str | None = None) -> JSONResponse:
            # A ``branch:directory`` tree-ish lists only that directory, with paths relative
            # to it; a path that names a blob is a 422 and a missing path a 404, as the real
            # Trees API answers.
            _, _, directory = tree_ish.partition(":")
            directory = unquote(directory).strip("/")
            prefix = f"{directory}/" if directory else ""
            with self._lock:
                if directory in self._objects:
                    return JSONResponse({"message": "Invalid tree"}, status_code=422)
                tree = [
                    {"path": p[len(prefix) :], "type": "blob", "mode": "100644", "sha": _blob_sha(self._objects[p])}
                    for p in sorted(self._objects)
                    if p.startswith(prefix)
                ]
            if directory and not tree:
                return JSONResponse({"message": "Not Found"}, status_code=404)
            return JSONResponse({"sha": _blob_sha(b""), "truncated": False, "tree": tree})

        return app
