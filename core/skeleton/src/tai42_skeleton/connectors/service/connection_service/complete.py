"""Finish a connection from the OAuth callback.

Exchange the code, persist the record (fresh connect), or replace tokens on an existing record
(reconnect / toggle-on), and reconcile the managed manifest.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.service import CompleteConnectResult

from tai42_skeleton.app.bus.ops import ReprobeFailedMcps
from tai42_skeleton.connectors.oauth import authorization, state
from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth.discovery import same_issuer
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.service import manifest_writer
from tai42_skeleton.connectors.store import client_store

from .persist import _persist

logger = logging.getLogger(__name__)


async def complete_connect(
    *,
    flow_id: str,
    code: str | None,
    iss: str | None = None,
    error: str | None = None,
) -> CompleteConnectResult:
    """Finish a login from its redirect: check the issuer, exchange the code, persist, write manifest entries.

    The flow record is single-use and is consumed first. Before ``error`` or ``code`` is acted on,
    the redirect's ``iss`` is checked against the issuer the login was started with (a mismatch
    raises :class:`~tai42_skeleton.connectors.oauth.authorization.OAuthIssuerMismatchError`); an
    accepted ``error`` raises ``OAuthAuthorizationCancelledError``. The token-exchange
    ``redirect_uri`` is the one stored at authorize-start (byte-identical per RFC 6749), and the
    login's recorded issuer, resource and client are the truth for this exchange: when the
    provider's authorization now resolves to another issuer or resource the completion fails
    before any token request.
    """
    flow_state = await state.get_and_delete(flow_id)
    if flow_state is None:
        raise oauth_client.OAuthError("state mismatch: no flow record found for the given flow_id")

    authorization.check_redirect_issuer(
        recorded_issuer=flow_state.issuer, iss_required=flow_state.iss_required, iss=iss
    )
    if error is not None:
        # The provider's error text is neither acted on nor echoed: only its presence.
        raise authorization.OAuthAuthorizationCancelledError("the authorization server reported an error")
    if not code:
        raise oauth_client.OAuthError("the authorization response carries no code")

    try:
        descriptor = _svc.get_provider(flow_state.provider_id)
    except KeyError as exc:
        # The provider plugin was removed between authorize-start and completion;
        # surface it as a typed OAuthError so the router maps it to a 4xx failed
        # body instead of a raw 500.
        raise oauth_client.OAuthError(f"provider {flow_state.provider_id!r} is no longer registered") from exc

    # Defence in depth: re-validate the stored redirect_uri against the allow-list
    # at the token exchange, not only at authorize-start, so a redirect_uri that
    # fell off the allow-list since authorize-start cannot drive an exchange.
    oauth_client.validate_redirect_uri(flow_state.redirect_uri)

    resolved = await authorization.resolve_authorization(descriptor, bound_issuer=flow_state.issuer)
    if not same_issuer(flow_state.issuer, resolved.issuer):
        raise oauth_client.OAuthError(
            f"provider {descriptor.id!r} now resolves to issuer {resolved.issuer!r}; "
            f"the login was started with {flow_state.issuer!r}"
        )
    if resolved.resource != flow_state.resource:
        raise oauth_client.OAuthError(
            f"provider {descriptor.id!r} now resolves to resource {resolved.resource!r}; "
            f"the login was started for {flow_state.resource!r}"
        )
    client = await authorization.resolve_client(
        descriptor, resolved, redirect_uri=flow_state.redirect_uri, client_id=flow_state.client_id
    )

    # A declared provider's first Connect must return a refresh token. A
    # discovered provider's server may issue none; on RECONNECT / TOGGLE an
    # existing one may be inherited in _complete_reconnect_or_toggle.
    require_refresh_token = flow_state.operation == state.FlowOperation.CONNECT and not descriptor.is_discovered
    token_resp = await _exchange(
        descriptor=descriptor,
        resolved=resolved,
        client=client,
        code=code,
        flow_state=flow_state,
        require_refresh_token=require_refresh_token,
    )
    grant = authorization.grant_for(descriptor, resolved, client)

    if flow_state.operation in (
        state.FlowOperation.RECONNECT,
        state.FlowOperation.TOGGLE_SUBSERVICE_ON,
    ):
        return await _complete_reconnect_or_toggle(
            flow_state=flow_state,
            descriptor=descriptor,
            token_resp=token_resp,
            grant=grant,
            guarded=resolved.guarded,
        )

    account_identity = _extract_account_identity(token_resp.raw) or "unknown"

    connection_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    record = ConnectionRecord(
        connection_id=connection_id,
        provider_id=flow_state.provider_id,
        kind="oauth",
        alias=flow_state.alias,
        account_identity=account_identity,
        enabled_sub_services=list(flow_state.enabled_sub_services),
        granted_scopes=token_resp.granted_scopes or list(flow_state.requested_scopes),
        access_token=SecretStr(token_resp.access_token),
        refresh_token=SecretStr(token_resp.refresh_token) if token_resp.refresh_token is not None else None,
        access_token_expires_at=token_resp.expires_at,
        auth_health_state=AuthHealthState.HEALTHY,
        grant=grant,
        created_at=now,
    )

    # Persist + manifest-add under the connection lock (the id is freshly
    # generated, so no contention) so a concurrent disconnect cannot delete the
    # record between the persist and the add and strand the added entries.
    added: list[str] = []

    def add_entries(document: dict[str, Any]) -> None:
        added[:] = manifest_writer.add_managed_entries(
            document,
            descriptor=descriptor,
            enabled_sub_services=flow_state.enabled_sub_services,
            alias=flow_state.alias,
            connection_id=connection_id,
        )

    async with _svc.connection_lock(connection_id):
        # create_only guards against a duplicate connection_id (fresh UUID, so a
        # collision is a hard error rather than a silent overwrite). A collision on
        # the durable (provider_id, alias) uniqueness raises AliasInUseError here.
        try:
            await _persist(record, create_only=True)
        except Exception:
            # The just-issued grant is brand-new and unshared; the completion
            # failed before it was persisted, so revoke it upstream rather than
            # orphan a live consent. Best-effort, never masks the original error.
            await _revoke_fresh_grant(descriptor, resolved, client, token_resp)
            raise

        # Append the managed entries through the single pipeline: it validates,
        # persists, locally reloads, and broadcasts the reload to the whole fleet.
        applied = await _svc.ConfigService.from_app().apply_change(add_entries)

    return CompleteConnectResult(
        connection_id=connection_id,
        return_url=flow_state.return_url,
        operation=flow_state.operation,
        added_manifest_entries=added,
        removed_manifest_entries=[],
        fanout=applied.fanout,
        span_release_error=applied.span_release_error,
    )


async def _exchange(
    *,
    descriptor: ProviderDescriptor,
    resolved: authorization.ResolvedAuthorization,
    client: authorization.ResolvedClient,
    code: str,
    flow_state: state.OAuthFlowState,
    require_refresh_token: bool,
) -> oauth_client.TokenResponse:
    """Exchange the login's code; a dynamic client the server answers ``invalid_client`` is marked dead first.

    The registration is gone, so the user's next attempt registers a new client. A manual client
    is never marked: an administrator corrects it.
    """
    try:
        return await oauth_client.exchange_code(
            descriptor=descriptor,
            resolved=resolved,
            client=client,
            code=code,
            code_verifier=flow_state.pkce_verifier,
            redirect_uri=flow_state.redirect_uri,
            resource=flow_state.resource,
            require_refresh_token=require_refresh_token,
        )
    except oauth_client.CodeExchangeFailedError as exc:
        if exc.error_code == "invalid_client" and client.source == "dynamic" and client.client_row_id is not None:
            await client_store.active_client_store().mark_dead(client.client_row_id)
            logger.warning(
                "connectors: the authorization server refused the dynamic client of provider %s; marked it dead",
                descriptor.id,
            )
        raise


async def _revoke_fresh_grant(
    descriptor: ProviderDescriptor,
    resolved: authorization.ResolvedAuthorization,
    client: authorization.ResolvedClient,
    token_resp: oauth_client.TokenResponse,
) -> None:
    """Best-effort upstream revoke of a brand-new, unshared grant whose fresh Connect failed after the code exchange.

    Revokes at the login's own authorization server with the login's own client (there is no stored
    record to read): the refresh token when the answer carried one, else the access token. Only
    ever called on the fresh-CONNECT path: that grant is unshared (a reconnect/toggle may inherit an
    existing record's token, so revoking it could kill a live connection — those paths deliberately
    do NOT revoke). Logged, never raises, never masks the caller's original error.
    """
    token = token_resp.refresh_token or token_resp.access_token
    try:
        outcome = await oauth_client.revoke(descriptor=descriptor, resolved=resolved, client=client, token=token)
        logger.warning(
            "connectors: revoked orphaned fresh grant for provider %s (outcome=%s)",
            descriptor.id,
            outcome.outcome,
        )
    except Exception:
        logger.warning(
            "connectors: best-effort revoke of orphaned fresh grant for provider %s raised",
            descriptor.id,
            exc_info=True,
        )


async def _complete_reconnect_or_toggle(
    *,
    flow_state: state.OAuthFlowState,
    descriptor: ProviderDescriptor,
    token_resp: oauth_client.TokenResponse,
    grant: ConnectionGrant | None,
    guarded: bool,
) -> CompleteConnectResult:
    """Replace the grant, tokens, scopes and enabled_sub_services on an existing record, under the connection lock.

    The old refresh token is kept, when the answer carries none, only if the new grant has the
    record's issuer and client; otherwise the record takes the answer's tokens as they are. A
    successful reconnect clears ``scopes_wanted``. Reconciles its managed manifest entries.

    A reconnect restores a credential the last listing may have failed on: once the record is
    persisted and the manifest reconciled, the connection's failed entries are re-probed on every
    worker now, not at the re-probe loop's next pass (a ``reprobe_failed_mcps`` fleet op; a worker
    where they are bound does nothing). The record is already persisted, so the siblings are asked
    even when this worker's pass fails, and that failure then raises
    :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError`.
    """
    cid = flow_state.reconnect_connection_id
    if not cid:
        raise oauth_client.OAuthError(f"flow operation {flow_state.operation} requires reconnect_connection_id")

    removed: list[str] = []
    added: list[str] = []
    async with _svc.connection_lock(cid):
        record, started_blob = await _svc.load_record_with_blob(cid)

        same_grant = _same_grant(record.grant, grant)
        if token_resp.refresh_token:
            record.refresh_token = SecretStr(token_resp.refresh_token)
        elif not same_grant:
            record.refresh_token = None
        record.access_token = SecretStr(token_resp.access_token)
        expires_at = token_resp.expires_at
        if expires_at is None and record.refresh_token is not None:
            # An inherited refresh token makes an answer without ``expires_in`` assume the default life.
            expires_at = oauth_client.expiry_without_expires_in(has_refresh_token=True, guarded=guarded)
        record.access_token_expires_at = expires_at
        record.grant = grant
        record.scopes_wanted = []
        # A provider that omits ``scope`` on the exchange is treated as granting
        # exactly what this flow requested (mirrors the CONNECT path), never as
        # revoking every scope.
        record.granted_scopes = list(token_resp.granted_scopes or flow_state.requested_scopes)
        record.auth_health_state = AuthHealthState.HEALTHY

        # Reconcile enabled_sub_services + manifest in lock-step. A RECONNECT may
        # carry a smaller set than before (validated non-empty + known, but not a
        # superset), so de-selected sub-services lose their entries too. For
        # TOGGLE_SUBSERVICE_ON to_remove is empty — patch_sub_services already
        # applied removals before forking to the consent flow.
        new_enabled = sorted(set(flow_state.enabled_sub_services))
        prior_enabled = set(record.enabled_sub_services)
        to_remove = prior_enabled - set(new_enabled)
        record.enabled_sub_services = new_enabled

        # Compare-and-set against the ciphertext this operation loaded: the
        # lock is best-effort, so a peer (e.g. a token refresh that rotated the
        # refresh token) may have written meanwhile — losing the CAS raises
        # rather than clobbering the peer's record.
        await _persist(record, expected_blob=started_blob)

        # Recovery: an explicit reconnect restores fresh tokens + HEALTHY, so drop
        # any refresh-cooldown breaker a prior failing run armed — otherwise a
        # fresh token already inside the safety margin would be fast-failed by the
        # still-live cooldown until it expires.
        await _svc.clear_refresh_cooldown(cid)

        # Reconcile the managed manifest in ONE pipeline transaction (remove +
        # append in a single mutator ⇒ one persist, one reload, one broadcast)
        # INSIDE the lock so a concurrent disconnect (which also takes the lock)
        # cannot delete the connection between this persist and the reconcile and
        # strand the added entries.
        def reconcile(document: dict[str, Any]) -> None:
            removed[:] = (
                manifest_writer.remove_managed_entries(document, connection_id=cid, sub_services=to_remove)
                if to_remove
                else []
            )
            added[:] = manifest_writer.add_managed_entries(
                document,
                descriptor=descriptor,
                enabled_sub_services=new_enabled,
                alias=record.alias,
                connection_id=cid,
            )

        # The pipeline runs only when the manifest changes (it writes and reloads whatever it is
        # given), and its fan-out is reported only then; the re-probe below is always asked for.
        # The whole enabled set is reconciled, so an owned entry the run-time check refuses (it fell
        # behind its descriptor) is rewritten by the reconnect.
        config_service = _svc.ConfigService.from_app()
        pending = manifest_writer.managed_entries_to_write(
            config_service.read_manifest_document(),
            descriptor=descriptor,
            enabled_sub_services=new_enabled,
            alias=record.alias,
            connection_id=cid,
        )
        applied = await config_service.apply_change(reconcile) if to_remove or pending else None

    titles = [manifest_writer.managed_title(descriptor.id, sub_service, record.alias) for sub_service in new_enabled]
    await _svc.broadcast(
        ReprobeFailedMcps(op="reprobe_failed_mcps", titles=titles),
        None,
        lambda: tai42_app.admin.reprobe_failed_mcps(titles),
        publish_on_local_failure=True,
    )

    return CompleteConnectResult(
        connection_id=cid,
        return_url=flow_state.return_url,
        operation=flow_state.operation,
        added_manifest_entries=added,
        removed_manifest_entries=removed,
        fanout=None if applied is None else applied.fanout,
        span_release_error=None if applied is None else applied.span_release_error,
    )


def _same_grant(old: ConnectionGrant | None, new: ConnectionGrant | None) -> bool:
    """Whether two grants come from the same authorization server and client (two ``None`` grants are equal)."""
    if old is None or new is None:
        return old is None and new is None
    return same_issuer(old.issuer, new.issuer) and old.client_id == new.client_id


def _extract_account_identity(raw_token_payload: dict) -> str | None:
    """Recover the connected account's email from an id_token (best-effort).

    The id_token is decoded WITHOUT signature verification — it was just returned
    over the TLS channel we initiated, so its origin is trusted enough for
    display only. Auth decisions rely on the access token.
    """
    id_token = raw_token_payload.get("id_token")
    if not id_token:
        return None
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email")
    except Exception:
        logger.warning(
            "connectors: id_token parse for account identity failed",
            exc_info=True,
        )
        return None
