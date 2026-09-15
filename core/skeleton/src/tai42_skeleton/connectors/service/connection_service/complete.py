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
from tai42_contract.connectors.models import AuthHealthState, ConnectionRecord
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.service import CompleteConnectResult

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth import state
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.service import manifest_writer

from .persist import _persist

logger = logging.getLogger(__name__)


async def complete_connect(
    *,
    flow_id: str,
    code: str,
) -> CompleteConnectResult:
    """Exchange code for tokens, persist the encrypted ConnectionRecord, and write managed manifest entries.

    The token-exchange ``redirect_uri`` is the one stored in the flow state at authorize-start (byte-identical
    per RFC 6749), never recomputed from the completion request.
    """
    flow_state = await state.get_and_delete(flow_id)
    if flow_state is None:
        raise oauth_client.OAuthError("state mismatch: no flow record found for the given flow_id")

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

    # On RECONNECT / TOGGLE the provider may not re-issue a refresh_token; the
    # existing one is inherited in _complete_reconnect_or_toggle.
    require_refresh_token = flow_state.operation == state.FlowOperation.CONNECT
    token_resp = await oauth_client.exchange_code(
        descriptor=descriptor,
        code=code,
        code_verifier=flow_state.pkce_verifier,
        redirect_uri=flow_state.redirect_uri,
        require_refresh_token=require_refresh_token,
    )

    if flow_state.operation in (
        state.FlowOperation.RECONNECT,
        state.FlowOperation.TOGGLE_SUBSERVICE_ON,
    ):
        return await _complete_reconnect_or_toggle(
            flow_state=flow_state,
            descriptor=descriptor,
            token_resp=token_resp,
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
            await _revoke_fresh_grant(descriptor, token_resp)
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
    )


async def _revoke_fresh_grant(descriptor: ProviderDescriptor, token_resp: oauth_client.TokenResponse) -> None:
    """Best-effort upstream revoke of a brand-new, unshared grant whose fresh Connect failed after the code exchange.

    Only ever called on the fresh-CONNECT path: that ``refresh_token`` is unshared
    (a reconnect/toggle would inherit an existing record's token, so revoking it
    could kill a live connection — those paths deliberately do NOT revoke). Logged,
    never raises, never masks the caller's original error.
    """
    if token_resp.refresh_token is None:
        return
    try:
        outcome = await oauth_client.revoke(descriptor=descriptor, token=token_resp.refresh_token)
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
) -> CompleteConnectResult:
    """Replace tokens + scopes + enabled_sub_services on an existing record, under the connection lock.

    Reconciles its managed manifest entries.
    """
    cid = flow_state.reconnect_connection_id
    if not cid:
        raise oauth_client.OAuthError(f"flow operation {flow_state.operation} requires reconnect_connection_id")

    removed: list[str] = []
    added: list[str] = []
    async with _svc.connection_lock(cid):
        record, started_blob = await _svc.load_record_with_blob(cid)

        record.access_token = SecretStr(token_resp.access_token)
        record.access_token_expires_at = token_resp.expires_at
        if token_resp.refresh_token:
            record.refresh_token = SecretStr(token_resp.refresh_token)
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
        to_add = set(new_enabled) - prior_enabled
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
            added[:] = (
                manifest_writer.add_managed_entries(
                    document,
                    descriptor=descriptor,
                    enabled_sub_services=to_add,
                    alias=record.alias,
                    connection_id=cid,
                )
                if to_add
                else []
            )

        # No delta ⇒ no apply_change ran, so there is no broadcast to report; the
        # fanout is honestly ``None`` (this path mutated no manifest).
        fanout: dict[str, Any] | None = None
        if to_remove or to_add:
            fanout = (await _svc.ConfigService.from_app().apply_change(reconcile)).fanout

    return CompleteConnectResult(
        connection_id=cid,
        return_url=flow_state.return_url,
        operation=flow_state.operation,
        added_manifest_entries=added,
        removed_manifest_entries=removed,
        fanout=fanout,
    )


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
