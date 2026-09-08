"""S3 ``Storage`` backend over an S3 bucket.

S3 stores bytes and content-types natively; ``stat`` reports the stored
``ContentType`` and maps a missing object to ``FileNotFoundError``.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError
from tai42_contract.app import tai42_app
from tai42_contract.storage import ObjectStat, Storage, StoragePathConflictError, assert_not_root

from tai42_storage_s3.client import S3Client
from tai42_storage_s3.settings import s3_settings

logger = logging.getLogger(__name__)

# S3 error codes for a missing key across get/head operations.
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})

# S3 rejects a single ``delete_objects`` request carrying more than 1000 keys.
_DELETE_BATCH_SIZE = 1000

# Content-type stored for every text template upload.
_TEMPLATE_CONTENT_TYPE = "application/jinja2"


def _is_not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES


def _ancestor_keys(path: str) -> list[str]:
    """The ancestor id prefixes of ``path`` — ``"a/b/c"`` -> ``["a", "a/b"]``.

    Each names an id that, if stored as a file, would sit where ``path`` needs a
    directory, so an upload of ``path`` beneath it is a collision.
    """
    segments = [s for s in path.split("/") if s]
    return ["/".join(segments[:i]) for i in range(1, len(segments))]


def _bucket() -> str:
    """The configured target bucket; raises a clear config error when unset."""
    bucket = s3_settings().bucket
    if not bucket:
        raise RuntimeError("S3 storage is not configured: set STORAGE_S3_BUCKET to the target bucket.")
    return bucket


# Importing this module registers S3Storage as the app's storage provider.
@tai42_app.storage.register_storage
class S3Storage(Storage):
    async def load(self, path: str) -> str:
        return (await self.load_bytes(path)).decode("utf-8")

    async def load_bytes(self, path: str) -> bytes:
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(S3Client) as client:
            try:
                resp = await client.get_object(Bucket=bucket, Key=path)
            except ClientError as e:
                if _is_not_found(e):
                    raise FileNotFoundError(f"Object not found: {path}") from e
                raise
            async with resp["Body"] as stream:
                data: bytes = await stream.read()
                return data

    async def list(self) -> list[str]:
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(S3Client) as client:
            paginator = client.get_paginator("list_objects_v2")
            keys: list[str] = []
            async for page in paginator.paginate(Bucket=bucket):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
            return keys

    async def upload(self, path: str, content: str) -> None:
        await self.upload_bytes(path, content.encode("utf-8"), content_type=_TEMPLATE_CONTENT_TYPE)

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        bucket = _bucket()
        put_kwargs: dict[str, Any] = {"Bucket": bucket, "Key": path, "Body": data}
        if content_type is not None:
            put_kwargs["ContentType"] = content_type
        async with tai42_app.clients.client_ctx(S3Client) as client:
            await self._assert_no_path_conflict(client, bucket, path)
            await client.put_object(**put_kwargs)
        logger.info("Uploaded object to %s", path)

    @staticmethod
    async def _assert_no_path_conflict(client: Any, bucket: str, path: str) -> None:
        """Refuse an upload whose ``path`` collides with the flat key space.

        Flat keys let ``a/b`` and ``a/b/c`` coexist silently, so one id ends up
        both a file and a directory. This refuses ``path`` when keys already live
        under its ``path + "/"`` prefix (``path`` would be a directory too) and when
        an ancestor id is itself a stored key (``path`` would be nested under a
        file), naming the conflicting ids.
        """
        paginator = client.get_paginator("list_objects_v2")
        under: list[str] = []
        async for page in paginator.paginate(Bucket=bucket, Prefix=f"{path}/"):
            for obj in page.get("Contents", []):
                under.append(obj["Key"])
        if under:
            raise StoragePathConflictError(path, sorted(under))
        for ancestor in _ancestor_keys(path):
            try:
                await client.head_object(Bucket=bucket, Key=ancestor)
            except ClientError as e:
                if _is_not_found(e):
                    continue
                raise
            raise StoragePathConflictError(path, [ancestor])

    async def delete(self, path: str) -> None:
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(S3Client) as client:
            # delete_object is silent on a missing key; confirm existence first
            # to honor the FileNotFoundError contract.
            try:
                await client.head_object(Bucket=bucket, Key=path)
            except ClientError as e:
                if _is_not_found(e):
                    raise FileNotFoundError(f"Object not found: {path}") from e
                raise
            await client.delete_object(Bucket=bucket, Key=path)
        logger.info("Deleted object %s", path)

    async def delete_dir(self, path: str) -> None:
        assert_not_root(path)

        # Treat the path as a directory prefix so a bare "d" can't match sibling
        # keys like "d2/x.j2".
        prefix = path if path.endswith("/") else f"{path}/"
        bucket = _bucket()

        async with tai42_app.clients.client_ctx(S3Client) as client:
            paginator = client.get_paginator("list_objects_v2")
            keys: list[dict[str, str]] = []
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append({"Key": obj["Key"]})

            if not keys:
                raise FileNotFoundError(f"Object directory not found or empty: {path}")

            for start in range(0, len(keys), _DELETE_BATCH_SIZE):
                chunk = keys[start : start + _DELETE_BATCH_SIZE]
                resp = await client.delete_objects(Bucket=bucket, Delete={"Objects": chunk})
                errors = resp.get("Errors", [])
                if errors:
                    raise RuntimeError(f"Failed to delete some objects under {path}: {errors}")

        logger.info("Deleted %d objects under %s", len(keys), path)

    async def stat(self, path: str) -> ObjectStat:
        bucket = _bucket()
        async with tai42_app.clients.client_ctx(S3Client) as client:
            try:
                resp = await client.head_object(Bucket=bucket, Key=path)
            except ClientError as e:
                if _is_not_found(e):
                    raise FileNotFoundError(f"Object not found: {path}") from e
                raise
            return ObjectStat(content_type=resp.get("ContentType"))
