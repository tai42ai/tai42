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
