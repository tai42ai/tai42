"""GitHub-backed :class:`~tai42_contract.storage.Storage` provider.

``stat``, ``load`` and ``load_bytes`` are one Contents API call each at the
configured branch: the object form answers the size, the raw form every byte of
the file. ``load_range`` resolves the branch's head commit through the Git refs API
and reads the raw host at that commit with a ``Range`` header, so a ranged read
answers the branch's current bytes. Writes use the Contents API (base64) and are
capped below the API limit, raising before the request rather than truncating.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from typing import Final, NamedTuple

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.storage import ObjectStat, Storage, StoragePathConflictError, assert_not_root

from tai42_storage_github.client import GithubHttpxClient
from tai42_storage_github.settings import GithubStorageSettings, github_storage_settings

logger = logging.getLogger(__name__)

# Conservative upload cap below GitHub's Contents API limit; an oversize file
# raises before the request. Reads are uncapped (the raw form serves every byte).
MAX_UPLOAD_BYTES = 1024 * 1024

# The Contents API answer forms: the object form carries ``type`` and ``size``; the
# raw form is the file's bytes, and a directory answers its JSON listing instead.
_OBJECT_FORM: Final = "application/vnd.github.object+json"
_RAW_FORM: Final = "application/vnd.github.raw+json"
_JSON: Final = "application/json"


class _Blob(NamedTuple):
    """A file in the branch's tree: its path and the blob sha a write or delete names."""

    path: str
    sha: str


def _join(base: str, path: str) -> str:
    """Join ``path`` onto ``base``, dropping empty and stray-slash segments.

    A ``..`` segment is rejected loudly: preserved, it would climb out of the
    configured repo/branch base and let a caller-supplied path reach an arbitrary
    raw/contents URL (path traversal).
    """
    segments = [segment for segment in path.split("/") if segment]
    if ".." in segments:
        raise ValueError(f"path traversal ('..') is not allowed in storage path: {path!r}")
    joined = "/".join(segments)
    base = base.rstrip("/")
    return f"{base}/{joined}" if joined else base


def _raw_url(settings: GithubStorageSettings, commit: str, path: str) -> str:
    """The raw-host URL for ``path`` at ``commit``, from the configured raw base-URL template."""
    base = settings.raw_base_url.format(username=settings.username, repo=settings.repo, commit=commit)
    return _join(base, path)


def _contents_url(settings: GithubStorageSettings, path: str) -> str:
    """The Contents-API URL for ``path``, from the configured contents base-URL template."""
    base = settings.contents_api_url.format(username=settings.username, repo=settings.repo)
    return _join(base, path)


def _configured_settings() -> GithubStorageSettings:
    """The cached settings; raises a clear config error when owner/repo is unset.

    The error names the env vars.
    """
    settings = github_storage_settings()
    missing = [
        env
        for env, value in (("STORAGE_GITHUB_USERNAME", settings.username), ("STORAGE_GITHUB_REPO", settings.repo))
        if not value
    ]
    if missing:
        raise RuntimeError(f"GitHub storage is not configured: set {' and '.join(missing)} to the target repository.")
    return settings


def _auth_headers(settings: GithubStorageSettings) -> dict[str, str]:
    """Authorization header, present only when a token is configured.

    The token is read from the :class:`~pydantic.SecretStr` only here and never logged.
    """
    if settings.token is None:
        return {}
    return {"Authorization": f"Bearer {settings.token.get_secret_value()}"}


def _api_headers(settings: GithubStorageSettings, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        **_auth_headers(settings),
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _media_type(resp: httpx.Response) -> str:
    """The ``Content-Type`` media type, without its parameters (``; charset=utf-8``)."""
    return resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _raise_for_status(resp: httpx.Response, action: str, path: str, url: str) -> None:
    """Surface an HTTP error loudly; the log names the URL/status/body, never the token."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        logger.exception(
            "HTTP error %s %s (%s): status=%s body=%s", action, path, url, resp.status_code, resp.text[:200]
        )
        raise


def _raise_unless_ok(resp: httpx.Response, expected: int, action: str, path: str, url: str) -> None:
    """Raise on any status other than ``expected``: an HTTP error loudly, an unexpected success as a fault."""
    if resp.status_code == expected:
        return
    _raise_for_status(resp, action, path, url)
    raise RuntimeError(f"GitHub answered {resp.status_code} {action} {path!r} at {url}; expected {expected}.")


# Importing this module registers GithubStorage as the active storage provider.
@tai42_app.storage.register_storage
class GithubStorage(Storage):
    """GitHub-backed storage: reads and writes via the Contents API at the branch, ranged reads via the raw host."""

    async def load(self, path: str) -> str:
        """Read ``path`` as UTF-8 text; a missing object raises ``FileNotFoundError``.

        The raw bytes are strictly decoded, so an object that is not UTF-8 text raises
        ``UnicodeDecodeError`` rather than reading back with substituted characters.
        """
        return (await self.load_bytes(path)).decode("utf-8")

    async def load_bytes(self, path: str) -> bytes:
        """Read every byte of ``path`` through the Contents API raw form; a missing object raises ``FileNotFoundError``.

        A directory answers its JSON listing instead of the raw form, and is not an object.
        """
        settings = _configured_settings()
        url = _contents_url(settings, path)
        async with tai42_app.clients.client_ctx(GithubHttpxClient) as client:
            resp = await client.get(url, headers=_api_headers(settings, _RAW_FORM), params={"ref": settings.branch})
            if resp.status_code == 404:
                raise FileNotFoundError(f"Object not found: {path}")
            _raise_unless_ok(resp, 200, "loading", path, url)
            media_type = _media_type(resp)
            if media_type == _RAW_FORM:
                return resp.content
            if media_type == _JSON:
                raise FileNotFoundError(f"Object not found: {path}")
            raise RuntimeError(f"GitHub answered an unexpected Content-Type {media_type!r} loading {path!r} at {url}.")

    async def stat(self, path: str) -> ObjectStat:
        """The byte size from the Contents API object form and the path-inferred content type.

        GitHub stores no content type. A missing object, and a directory (not an object),
        raise ``FileNotFoundError``; a file answer without an integer size raises ``RuntimeError``.
        """
        settings = _configured_settings()
        url = _contents_url(settings, path)
        async with tai42_app.clients.client_ctx(GithubHttpxClient) as client:
            resp = await client.get(url, headers=_api_headers(settings, _OBJECT_FORM), params={"ref": settings.branch})
            if resp.status_code == 404:
                raise FileNotFoundError(f"Object not found: {path}")
            _raise_unless_ok(resp, 200, "reading the size of", path, url)
            data = resp.json()
        if not isinstance(data, dict) or data.get("type") != "file":
            raise FileNotFoundError(f"Object not found: {path}")
        size = data.get("size")
        if type(size) is not int:
            raise RuntimeError(f"GitHub Contents API answered a file without an integer size at {url}.")
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type, size=size)

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        """At most ``length`` bytes of ``path`` from ``start``, read at the branch's current head commit.

        Resolves the head commit, then reads the raw host at that commit with a ``Range``
        header and the identity encoding, since a compressed answer ranges over the encoded
        bytes. ``b""`` for ``length == 0`` (no request) and for a start at or past the end
        (416); ``FileNotFoundError`` for a missing object or a directory (404). An answer
        that ignores the range (200), is still encoded, or runs longer than ``length``
        raises ``RuntimeError``.
        """
        if length == 0:
            return b""
        settings = _configured_settings()
        async with tai42_app.clients.client_ctx(GithubHttpxClient) as client:
            commit = await self._head_commit(client, settings, path)
            url = _raw_url(settings, commit, path)
            headers = {
                **_auth_headers(settings),
                "Accept-Encoding": "identity",
                "Range": f"bytes={start}-{start + length - 1}",
            }
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 404:
                    raise FileNotFoundError(f"Object not found: {path}")
                if resp.status_code == 416:
                    return b""
                if resp.status_code == 200:
                    raise RuntimeError(f"GitHub answered 200, the whole object, to a ranged read of {url}.")
                if resp.status_code != 206:
                    await resp.aread()
                    _raise_unless_ok(resp, 206, "loading a range of", path, url)
                encoding = resp.headers.get("content-encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise RuntimeError(
                        f"GitHub answered a {encoding}-encoded range of {url}; the identity encoding was asked."
                    )
                chunks: list[bytes] = []
                received = 0
                async for chunk in resp.aiter_bytes():
                    received += len(chunk)
                    if received > length:
                        raise RuntimeError(f"GitHub answered more than the {length} bytes asked of {url}.")
                    chunks.append(chunk)
                return b"".join(chunks)

    @staticmethod
    async def _head_commit(client: httpx.AsyncClient, settings: GithubStorageSettings, path: str) -> str:
        """The branch's current head commit sha, from the Git refs API."""
        url = settings.ref_api_url.format(username=settings.username, repo=settings.repo, branch=settings.branch)
        resp = await client.get(url, headers=_api_headers(settings))
        _raise_unless_ok(resp, 200, "resolving the branch head for", path, url)
        data = resp.json()
        target = data.get("object") if isinstance(data, dict) else None
        sha = target.get("sha") if isinstance(target, dict) else None
        if not isinstance(sha, str) or not sha:
            raise RuntimeError(f"GitHub answered no head commit sha for the branch at {url}.")
        return sha

    async def list(self) -> list[str]:
        """Every blob path in the repo."""
        return [blob.path for blob in await self._blobs("")]

    async def _blobs(self, prefix: str) -> list[_Blob]:
        """Every blob in the repo with its blob sha, optionally filtered to ``prefix``.

        Uses the recursive Git Trees API (one request); its ``truncated`` flag is
        raised on loudly rather than acting on a partial listing, and so is a blob
        entry carrying no sha.
        """
        settings = _configured_settings()
        url = settings.trees_api_url.format(username=settings.username, repo=settings.repo, branch=settings.branch)
        async with tai42_app.clients.client_ctx(GithubHttpxClient) as client:
            resp = await client.get(url, headers=_api_headers(settings), params={"recursive": "1"})
            _raise_for_status(resp, "listing", prefix or "/", url)
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"Expected a tree object from {url}, got {type(data).__name__}")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
            if data.get("truncated"):
                raise RuntimeError(f"GitHub tree listing was truncated; the tree is too large to list safely: {url}")
            blobs: list[_Blob] = []
            for item in data.get("tree", []):
                if not isinstance(item, dict) or item.get("type") != "blob":
                    continue
                path = item["path"]
                if prefix and not path.startswith(prefix):
                    continue
                sha = item.get("sha")
                if not isinstance(sha, str) or not sha:
                    raise RuntimeError(f"GitHub tree listing at {url} carries no blob sha for {path}.")
                blobs.append(_Blob(path, sha))
            return blobs

    async def upload(self, path: str, content: str) -> None:
        """Write ``content`` (UTF-8) to ``path`` via the Contents API."""
        await self._put_contents(path, content.encode("utf-8"))

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Write ``data`` to ``path`` via the Contents API; ``content_type`` is unused (GitHub stores none)."""
        # GitHub stores no per-object content-type; content_type is unused.
        await self._put_contents(path, data)

    async def _put_contents(self, path: str, data: bytes) -> None:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"Refusing to upload {len(data)} bytes to {path!r}: exceeds the conservative "
                f"{MAX_UPLOAD_BYTES}-byte GitHub Contents API upload cap. Store large objects elsewhere."
            )
        settings = _configured_settings()
        url = _contents_url(settings, path)
        headers = _api_headers(settings)
        encoded = base64.b64encode(data).decode("ascii")
        async with tai42_app.clients.client_ctx(GithubHttpxClient) as client:
            sha: str | None = None
            # A 404 means the object doesn't exist yet (a create); any other
            # failure must surface, not be mistaken for a new object.
            get_resp = await client.get(url, headers=headers, params={"ref": settings.branch})
            if get_resp.status_code == 200:
                existing = get_resp.json()
                if isinstance(existing, list):
                    # The Contents API returns an array for a directory: the id names a
                    # directory that holds objects and cannot also become a file.
                    conflicts = sorted(item["path"] for item in existing if isinstance(item, dict) and "path" in item)
                    raise StoragePathConflictError(path, conflicts)
                if isinstance(existing, dict):
                    sha = existing.get("sha")
            elif get_resp.status_code != 404:
                _raise_for_status(get_resp, "resolving for upload", path, url)

            payload: dict[str, str] = {"message": f"Update {path}", "content": encoded, "branch": settings.branch}
            if sha:
                payload["sha"] = sha
            resp = await client.put(url, headers=headers, json=payload)
            # The 422 backstop: an oversize/invalid payload the API itself rejects.
            _raise_for_status(resp, "uploading", path, url)

    async def delete(self, path: str) -> None:
        """Delete the file at ``path``; a missing object raises ``FileNotFoundError``."""
        settings = _configured_settings()
        url = _contents_url(settings, path)
        headers = _api_headers(settings)
        async with tai42_app.clients.client_ctx(GithubHttpxClient) as client:
            get_resp = await client.get(url, headers=headers, params={"ref": settings.branch})
            if get_resp.status_code == 404:
                raise FileNotFoundError(f"Object not found: {path}")
            _raise_for_status(get_resp, "resolving for delete", path, url)

            data = get_resp.json()
            # The Contents API returns a list for a directory and an object for a
            # file; only a file has a deletable blob sha.
            if not isinstance(data, dict):
                raise FileNotFoundError(f"Object not found: {path}")

            sha = data.get("sha")
            # The delete payload requires the blob sha; a file object without one
            # is an unexpected response, raised on here.
            if not sha:
                raise RuntimeError(f"GitHub Contents API returned no blob sha for {path}; cannot delete it.")

            await self._delete_blob(client, settings, path, sha)

    async def delete_dir(self, path: str) -> None:
        """Delete every file under ``path`` sequentially (GitHub has no bulk delete).

        The one tree listing supplies each file's blob sha, so each file costs one
        DELETE. A file already gone is skipped; a file changed since the listing
        answers 409, which raises like every other failure mid-loop, so a partial
        deletion surfaces loudly.
        """
        assert_not_root(path)
        prefix = path if path.endswith("/") else f"{path}/"
        blobs = await self._blobs(prefix)
        if not blobs:
            raise FileNotFoundError(f"Directory not found or empty: {path}")

        settings = _configured_settings()
        async with tai42_app.clients.client_ctx(GithubHttpxClient) as client:
            for blob in blobs:
                try:
                    await self._delete_blob(client, settings, blob.path, blob.sha)
                except FileNotFoundError:
                    # Already gone (concurrent delete); the delete is idempotent. A
                    # real DELETE failure raises HTTPStatusError and is not caught here.
                    logger.info("Object %s already gone during dir delete of %s; skipping", blob.path, path)

    @staticmethod
    async def _delete_blob(client: httpx.AsyncClient, settings: GithubStorageSettings, path: str, sha: str) -> None:
        """Delete the file at ``path`` holding blob ``sha``; a missing object raises ``FileNotFoundError``."""
        url = _contents_url(settings, path)
        payload = {"message": f"Delete {path}", "sha": sha, "branch": settings.branch}
        resp = await client.request("DELETE", url, headers=_api_headers(settings), json=payload)
        if resp.status_code == 404:
            raise FileNotFoundError(f"Object not found: {path}")
        _raise_for_status(resp, "deleting", path, url)


__all__ = ["GithubStorage"]
