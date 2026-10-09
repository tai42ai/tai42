"""The managed-connector resolve probe: drives the real resolver path so the
refresh-lock test can read one upstream refresh across concurrent replica calls.
Registers at import via ``@tai42_app.tools.tool``."""

from __future__ import annotations

import os

from tai42_contract.app import tai42_app


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_resolve_connector(connection_id: str, provider_id: str = "e2e_idp", sub_service: str = "default") -> dict:
    """Resolve a managed-connector token for ``connection_id`` in this process.

    Drives the real resolver path (freshness gate + refresh-under-lock + CAS
    write-back); an expired stored token forces one refresh serialized by the
    cross-process connection lock. Two concurrent calls across replicas therefore
    trigger exactly one upstream refresh — the seam the refresh-lock test reads
    off the stub IdP's refresh counter."""
    from tai42_skeleton.connectors.runtime.resolver import resolve_managed_auth

    auth = await resolve_managed_auth(connection_id, provider_id, sub_service)
    return {"resolved": auth is not None, "pid": os.getpid()}


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_age_connection_token(connection_id: str, seconds_ago: int = 60) -> dict:
    """Move the STORED access-token expiry of ``connection_id`` ``seconds_ago`` into the past.

    The token itself, and the sign-in service's own record of it, are untouched: the platform
    believes its token expired, so its next use (or a reload's credential step) refreshes it.
    Written with the store's compare-and-set on the blob it read; a lost race raises."""
    from datetime import UTC, datetime, timedelta

    from tai42_skeleton.connectors.oauth import crypto
    from tai42_skeleton.connectors.store import token_store
    from tai42_skeleton.connectors.store.persistence import load_record_with_blob, session_expires_at_for

    record, blob = await load_record_with_blob(connection_id)
    aged = record.model_copy(update={"access_token_expires_at": datetime.now(UTC) - timedelta(seconds=seconds_ago)})
    committed = await token_store().put(
        connection_id,
        crypto.encrypt(aged.to_storage_json().encode("utf-8"), aad=connection_id),
        expected_blob=blob,
        session_expires_at=session_expires_at_for(aged),
        provider_id=aged.provider_id,
        alias=aged.alias,
    )
    if not committed:
        raise RuntimeError(f"connection {connection_id} changed while its expiry was being moved")
    return {"aged": True}
