"""OCI ``Storage`` backend over an Object Storage bucket.

Object Storage stores bytes and content-types natively; ``stat`` reports the
stored type and maps a missing object to ``FileNotFoundError``. Unlike S3 there
is no batch delete in the API, so ``delete_dir`` removes objects one at a time.
"""

from __future__ import annotations

import logging
from typing import Final

from oci.exceptions import ServiceError
from tai42_contract.app import tai42_app
from tai42_contract.storage import ObjectStat, Storage, StoragePathConflictError, assert_not_root

from tai42_storage_oci.client import OciClient, OciObjectStorage
from tai42_storage_oci.settings import oci_settings

logger = logging.getLogger(__name__)

# The HTTP status Object Storage returns for a missing object or bucket.
_NOT_FOUND_STATUS: Final = 404

# The type of a UTF-8 text body; a caller wanting another type passes it to ``upload_bytes``.
_TEXT_CONTENT_TYPE: Final = "text/plain; charset=utf-8"


def _is_not_found(error: ServiceError) -> bool:
    return error.status == _NOT_FOUND_STATUS


def _ancestor_keys(path: str) -> list[str]:
    """The ancestor id prefixes of ``path`` — ``"a/b/c"`` -> ``["a", "a/b"]``.

    Each names an id that, if stored as an object, would sit where ``path`` needs
    a directory, so an upload of ``path`` beneath it is a collision.
    """
    segments = [s for s in path.split("/") if s]
    return ["/".join(segments[:i]) for i in range(1, len(segments))]


def _bucket() -> str:
    """The configured target bucket; raises a clear config error when unset."""
    bucket = oci_settings().bucket
    if not bucket:
        raise RuntimeError("OCI storage is not configured: set STORAGE_OCI_BUCKET to the target bucket.")
    return bucket


# Importing this module registers OciStorage as the app's storage provider.
@tai42_app.storage.register_storage
class OciStorage(Storage):
    """OCI-backed :class:`Storage` provider — objects are flat names in the configured bucket."""

    async def load(self, path: str) -> str:
        """Load ``path`` as UTF-8 text, raising ``FileNotFoundError`` when absent."""
        return (await self.load_bytes(path)).decode("utf-8")

    async def load_bytes(self, path: str) -> bytes:
        """Load ``path``'s raw bytes, raising ``FileNotFoundError`` when absent."""
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(OciClient) as client:
            try:
                data, _ = await client.get_object(bucket, path)
            except ServiceError as e:
                if _is_not_found(e):
                    raise FileNotFoundError(f"Object not found: {path}") from e
                raise
            return data

    async def list(self) -> list[str]:
        """Every object name in the bucket."""
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(OciClient) as client:
            return await client.list_objects(bucket)

    async def upload(self, path: str, content: str) -> None:
        """Upload ``content`` as UTF-8 text at ``path``."""
        await self.upload_bytes(path, content.encode("utf-8"), content_type=_TEXT_CONTENT_TYPE)

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Upload ``data`` at ``path`` with an optional ``content_type``, refusing a flat-name collision."""
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(OciClient) as client:
            await self._assert_no_path_conflict(client, bucket, path)
            await client.put_object(bucket, path, data, content_type)
        logger.info("Uploaded object to %s", path)

    @staticmethod
    async def _assert_no_path_conflict(client: OciObjectStorage, bucket: str, path: str) -> None:
        """Refuse an upload whose ``path`` collides with the flat name space.

        Flat names let ``a/b`` and ``a/b/c`` coexist silently, so one id ends up
        both a file and a directory. This refuses ``path`` when objects already
        live under its ``path + "/"`` prefix (``path`` would be a directory too)
        and when an ancestor id is itself a stored object (``path`` would be
        nested under a file), naming the conflicting ids.
        """
        under = await client.list_objects(bucket, prefix=f"{path}/")
        if under:
            raise StoragePathConflictError(path, sorted(under))
        for ancestor in _ancestor_keys(path):
            try:
                await client.head_object(bucket, ancestor)
            except ServiceError as e:
                if _is_not_found(e):
                    continue
                raise
            raise StoragePathConflictError(path, [ancestor])

    async def delete(self, path: str) -> None:
        """Delete the object at ``path``, raising ``FileNotFoundError`` when absent."""
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(OciClient) as client:
            try:
                await client.delete_object(bucket, path)
            except ServiceError as e:
                if _is_not_found(e):
                    raise FileNotFoundError(f"Object not found: {path}") from e
                raise
        logger.info("Deleted object %s", path)

    async def delete_dir(self, path: str) -> None:
        """Delete every object under the ``path`` directory prefix, raising ``FileNotFoundError`` when none."""
        assert_not_root(path)

        # Treat the path as a directory prefix so a bare "d" can't match sibling
        # names like "d2/x.j2".
        prefix = path if path.endswith("/") else f"{path}/"
        bucket = _bucket()

        async with tai42_app.clients.client_ctx(OciClient) as client:
            names = await client.list_objects(bucket, prefix=prefix)
            if not names:
                raise FileNotFoundError(f"Object directory not found or empty: {path}")
            # The API has no batch delete, so each object goes on its own call.
            # A concurrent delete of one of them surfaces as a 404 mid-loop and
            # propagates: the contract only promises pre-mutation validation.
            for name in names:
                await client.delete_object(bucket, name)

        logger.info("Deleted %d objects under %s", len(names), path)

    async def stat(self, path: str) -> ObjectStat:
        """The :class:`ObjectStat` for ``path``, raising ``FileNotFoundError`` when absent."""
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(OciClient) as client:
            try:
                content_type = await client.head_object(bucket, path)
            except ServiceError as e:
                if _is_not_found(e):
                    raise FileNotFoundError(f"Object not found: {path}") from e
                raise
            return ObjectStat(content_type=content_type)
