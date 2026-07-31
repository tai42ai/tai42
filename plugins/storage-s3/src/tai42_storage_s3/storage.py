"""S3 ``Storage`` backend over an S3 bucket.

S3 stores bytes and content-types natively; ``stat`` reports the stored
``ContentType`` and maps a missing object to ``FileNotFoundError``.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError
from tai42_contract.app import tai42_app
from tai42_contract.storage import ObjectStat, Storage, assert_not_root

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
        put_kwargs: dict[str, Any] = {"Bucket": _bucket(), "Key": path, "Body": data}
        if content_type is not None:
            put_kwargs["ContentType"] = content_type
        async with tai42_app.clients.client_ctx(S3Client) as client:
            await client.put_object(**put_kwargs)
        logger.info("Uploaded object to %s", path)

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
