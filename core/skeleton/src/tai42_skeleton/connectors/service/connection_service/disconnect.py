"""Tear down a connection under the connection lock.

Best-effort upstream revoke, then purge the encrypted blob and remove its managed
manifest entries.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.connectors.errors import ConnectorClientRequiredError, OperatorMisconfiguredError
from tai42_contract.connectors.models import ConnectionRecord
from tai42_contract.connectors.service import DisconnectResult

from tai42_skeleton.connectors.oauth import authorization
from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth.discovery import OAuthDiscoveryError
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.service import manifest_writer

logger = logging.getLogger(__name__)


async def disconnect(
    *,
    connection_id: str,
) -> DisconnectResult:
    """Disconnect a connection: best-effort upstream revoke, then purge the blob and managed manifest entries.

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
        # upstream revoke (and the catalog get_provider lookup) entirely.
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
                span_release_error=applied.span_release_error,
            )

        revoke_outcome = await _revoke_upstream(record)

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
        span_release_error=applied.span_release_error,
    )


async def _revoke_upstream(record: ConnectionRecord) -> oauth_client.RevokeOutcome:
    """Best-effort revoke of an OAuth record's grant; never raises, so the local purge always runs.

    The token revoked is the refresh token when the record has one, else the access token, sent to
    the revocation endpoint of the authorization server the grant is bound to with the grant's
    client. When the grant no longer belongs to what the descriptor names (another issuer or
    client), or its authorization or client cannot be resolved, nothing is sent and the outcome is
    ``failed``; a provider that is gone, or no revocation endpoint, is ``skipped``.
    """
    connection_id = record.connection_id
    token_secret = record.refresh_token or record.access_token
    if token_secret is None:
        logger.warning("connectors: oauth record %s holds no token to revoke", connection_id)
        return oauth_client.RevokeOutcome(outcome="skipped")
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
        return oauth_client.RevokeOutcome(outcome="skipped")
    try:
        target = await authorization.resolve_for_grant(descriptor, record.grant)
        # A declared provider's revoke sends only the token, so it needs no client.
        client = (
            await authorization.resolve_client(descriptor, target.resolved, client_id=target.client_id)
            if target.resolved.guarded
            else None
        )
    except (
        authorization.GrantUnservableError,
        OAuthDiscoveryError,
        ConnectorClientRequiredError,
        OperatorMisconfiguredError,
    ) as exc:
        logger.warning(
            "connectors: not revoking the grant of connection %s upstream (%s: %s); purging locally",
            connection_id,
            type(exc).__name__,
            exc,
        )
        return oauth_client.RevokeOutcome(outcome="failed")
    return await oauth_client.revoke(
        descriptor=descriptor, resolved=target.resolved, client=client, token=token_secret.get_secret_value()
    )
