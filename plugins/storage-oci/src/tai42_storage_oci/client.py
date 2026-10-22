"""Pooled OCI Object Storage client.

The OCI SDK is synchronous, so ``OciClient`` pools an async facade over one
``ObjectStorageClient`` and runs every call on a worker thread. The facade keeps
``storage.py`` await-shaped and confines the threading to one place.

``ObjectStorageClient`` is not loop-bound, but the pool keys per event loop
anyway: the facade is what callers hold, and a single configured client per loop
matches the rest of the fleet. Its connection comes entirely from the cached
``oci_settings`` singleton, so the pool key stays empty.
"""

from __future__ import annotations

import asyncio
from typing import Any

import oci
from tai42_kit.clients.base import PooledClient, reject_unknown_connection_kwargs

from tai42_storage_oci.settings import OciSettings, oci_settings

# Connection comes entirely from settings, so no connection kwargs are accepted;
# anything passed would split the pool key and is rejected loudly.
_ALLOWED_KWARGS: frozenset[str] = frozenset()


def _signer(settings: OciSettings) -> Any:
    """The request signer for the configured auth mode, or ``None`` for ``config_file``.

    Each mode fails loudly when its prerequisite is absent: there is no fallback
    chain, so a misconfigured pod reports the mode it could not satisfy rather
    than silently authenticating as something else.
    """
    if settings.auth == "workload_identity":
        kwargs: dict[str, Any] = {}
        if settings.service_account_token_path is not None:
            kwargs["service_account_token_path"] = settings.service_account_token_path
        return oci.auth.signers.get_oke_workload_identity_resource_principal_signer(**kwargs)
    if settings.auth == "instance_principal":
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return None


def _config(settings: OciSettings) -> dict[str, Any]:
    """The SDK config dict for the configured auth mode.

    A principal carries its own tenancy, so the signer modes pass an empty
    config; ``config_file`` reads the developer's own profile. A set ``region``
    overrides either.
    """
    config: dict[str, Any] = dict(oci.config.from_file()) if settings.auth == "config_file" else {}
    if settings.region is not None:
        config["region"] = settings.region
    return config


class OciObjectStorage:
    """An await-shaped facade over the synchronous ``ObjectStorageClient``.

    Every method runs the SDK call on a worker thread. ``namespace`` is resolved
    once at construction so no request pays for it. Timeouts belong to the SDK
    client, not to a call, so no method carries one.
    """

    def __init__(self, client: Any, namespace: str) -> None:
        """Hold the SDK ``client`` and its resolved ``namespace``."""
        self._client = client
        self._namespace = namespace

    async def get_object(self, bucket: str, name: str) -> tuple[bytes, str | None]:
        """Return the object's bytes and stored content-type."""

        def call() -> tuple[bytes, str | None]:
            response = self._client.get_object(self._namespace, bucket, name)
            return response.data.content, response.headers.get("content-type")

        return await asyncio.to_thread(call)

    async def put_object(self, bucket: str, name: str, data: bytes, content_type: str | None) -> None:
        """Store ``data`` at ``name``, tagging ``content_type`` when given."""

        def call() -> None:
            kwargs: dict[str, Any] = {}
            if content_type is not None:
                kwargs["content_type"] = content_type
            self._client.put_object(self._namespace, bucket, name, data, **kwargs)

        await asyncio.to_thread(call)

    async def list_objects(self, bucket: str, prefix: str | None = None) -> list[str]:
        """Every object name under ``prefix``, following the paging cursor to the end."""

        def call() -> list[str]:
            names: list[str] = []
            start: str | None = None
            while True:
                kwargs: dict[str, Any] = {"fields": "name"}
                if prefix is not None:
                    kwargs["prefix"] = prefix
                if start is not None:
                    kwargs["start"] = start
                page = self._client.list_objects(self._namespace, bucket, **kwargs).data
                names.extend(o.name for o in page.objects)
                start = page.next_start_with
                if not start:
                    return names

        return await asyncio.to_thread(call)

    async def head_object(self, bucket: str, name: str) -> str | None:
        """The stored content-type for ``name``; raises when the object is absent."""

        def call() -> str | None:
            response = self._client.head_object(self._namespace, bucket, name)
            return response.headers.get("content-type")

        return await asyncio.to_thread(call)

    async def delete_object(self, bucket: str, name: str) -> None:
        """Delete ``name``; raises when the object is absent."""

        def call() -> None:
            self._client.delete_object(self._namespace, bucket, name)

        await asyncio.to_thread(call)


class OciClient(PooledClient[OciObjectStorage]):
    """A pooled OCI Object Storage facade, one per event loop, configured from ``oci_settings``."""

    async def _create(self, **kwargs: Any) -> OciObjectStorage:
        reject_unknown_connection_kwargs("OCI client", kwargs, _ALLOWED_KWARGS)
        settings = oci_settings()

        def build() -> OciObjectStorage:
            signer = _signer(settings)
            client_kwargs: dict[str, Any] = {"timeout": (settings.connect_timeout, settings.read_timeout)}
            if signer is not None:
                client_kwargs["signer"] = signer
            client = oci.object_storage.ObjectStorageClient(_config(settings), **client_kwargs)
            namespace = settings.namespace if settings.namespace is not None else client.get_namespace().data
            return OciObjectStorage(client, namespace)

        return await asyncio.to_thread(build)

    async def _close(self, client: OciObjectStorage) -> None:
        # The SDK client holds a requests session that needs no explicit close,
        # and the facade owns nothing else.
        return None

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        # The SDK raises RequestException for a dead connection. A ServiceError
        # (an API error over a healthy connection) is not one, so it never
        # evicts the pooled client.
        return (oci.exceptions.RequestException,)
