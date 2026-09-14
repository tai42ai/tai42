"""Tear down a connection: best-effort upstream revoke, then purge the encrypted blob
and remove its managed manifest entries under the connection lock."""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.connectors.errors import ConnectorError
from tai42_contract.connectors.service import DisconnectResult

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.service import manifest_writer

logger = logging.getLogger(__name__)


async def disconnect(
    *,
    connection_id: str,
) -> DisconnectResult:
    """Disconnect a connection: best-effort upstream revoke, then purge the
    encrypted blob and remove managed manifest entries.

    Runs under the connection lock so an in-flight ``patch`` / reconnect (which
    reconciles the manifest under the same lock) cannot add managed entries after
    this disconnect removed them — otherwise those entries would be stranded
    against a deleted connection.
    """
    async with _svc.connection_lock(connection_id):
        # Cleanup must reach an EXPIRED connection too: a lapsed session still has
        # an encrypted blob + managed manifest entries that only disconnect can
        # purge, so load with include_expired (serving reads keep filtering).
        record = await _svc.load_record(connection_id, include_expired=True)

        removed: list[str] = []

        def remove_entries(document: dict[str, Any]) -> None:
            removed[:] = manifest_writer.remove_managed_entries(document, connection_id=connection_id)

        # No-auth has no token to revoke and no provider creds to read — skip the
        # upstream revoke (and the catalog get_provider lookup) entirely, before
        # the token access below would crash on a None refresh_token.
        if record.kind == "none":
            # Remove the managed entries through the single pipeline (validate +
            # persist + reload + broadcast) before purging the blob.
            applied = await _svc.ConfigService.from_app().apply_change(remove_entries)
            await _svc.token_store().delete(connection_id)
            return DisconnectResult(
                connection_id=connection_id,
                upstream_revoke_outcome="skipped",
                upstream_revoke_status=None,
                removed_manifest_entries=removed,
                fanout=applied.fanout,
            )

        if record.refresh_token is None:
            # An oauth record without a refresh token is corrupt — fail loudly
            # rather than send an empty revocation upstream.
            raise ConnectorError(f"oauth record {connection_id} has no refresh_token")
        try:
            descriptor = _svc.get_provider(record.provider_id)
        except KeyError:
            # The provider plugin was removed, so there is nothing to revoke
            # against — but the local blob + manifest entries must still be
            # purgeable. Skip the upstream revoke and proceed with local removal.
            logger.warning(
                "connectors: provider %r for connection %s is no longer registered; "
                "skipping upstream revoke and purging locally",
                record.provider_id,
                connection_id,
            )
            revoke_outcome = oauth_client.RevokeOutcome(outcome="skipped")
        else:
            revoke_outcome = await oauth_client.revoke(
                descriptor=descriptor,
                token=record.refresh_token.get_secret_value(),
            )

        # Remove manifest entries first so the tool stops being callable
        # immediately, then purge the blob. revoke() already ran above, so a
        # crash between the two leaves only an already-revoked (dead) orphan
        # token blob — harmless, reclaimed by the session-TTL cache expiry or a
        # re-disconnect. The removal crosses the single pipeline (validate +
        # persist + reload + broadcast).
        applied = await _svc.ConfigService.from_app().apply_change(remove_entries)

        # "user clicked Disconnect" revokes access even if a peer just wrote new
        # tokens. delete is idempotent.
        await _svc.token_store().delete(connection_id)

    return DisconnectResult(
        connection_id=connection_id,
        upstream_revoke_outcome=revoke_outcome.outcome,
        upstream_revoke_status=revoke_outcome.http_status,
        removed_manifest_entries=removed,
        fanout=applied.fanout,
    )
