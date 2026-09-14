"""Toggle which sub-services are enabled on a connection: remove/add inline under the
lock, or fork a consent OAuth flow when a newly-enabled sub-service needs new scopes."""

from __future__ import annotations

from typing import Any

from tai42_contract.connectors.service import PatchResult

from tai42_skeleton.connectors.oauth import redirect, state
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.service import manifest_writer

from .flow import _start_flow
from .persist import _persist
from .validation import _scopes_for, _validate_return_url, _validate_sub_services


async def patch_sub_services(
    *,
    connection_id: str,
    desired: list[str],
    return_url: str,
    redirect_uri: str,
    origin: str,
) -> PatchResult:
    """Toggle which sub-services are enabled.

    Sub-services toggled OFF lose their manifest entries. Sub-services toggled
    ON whose scopes are already granted are recreated inline; otherwise a
    ``TOGGLE_SUBSERVICE_ON`` flow is started and the authorize URL returned for a
    consent popup that :func:`~tai42_skeleton.connectors.service.connection_service.complete.complete_connect`
    finalises.
    """
    _validate_return_url(return_url)

    # Mutate the record under the lock and BEFORE touching the manifest, so a
    # losing writer never tears manifest entries out against a record it failed
    # to persist.
    async with _svc.connection_lock(connection_id):
        record, started_blob = await _svc.load_record_with_blob(connection_id)
        try:
            descriptor = _svc.get_provider(record.provider_id)
        except KeyError as exc:
            # The provider plugin was removed since this connection was created;
            # surface a typed ValueError the router maps to a 4xx, not a raw 500.
            raise ValueError(f"unknown provider: {record.provider_id!r}") from exc
        _validate_sub_services(descriptor, desired)

        current = set(record.enabled_sub_services)
        desired_set = set(desired)
        if current == desired_set:
            raise ValueError("enabled_sub_services unchanged")

        to_remove = current - desired_set
        to_add = desired_set - current

        granted = set(record.granted_scopes)
        needs_consent_subs: list[str] = []
        can_inline_subs: list[str] = []
        for sub_id in to_add:
            # A no-auth connection has no OAuth endpoints, so there is no
            # consent flow to fork — every toggle is inline regardless of any
            # scopes the descriptor declares.
            if record.kind == "none" or set(descriptor.sub_services[sub_id].scopes).issubset(granted):
                can_inline_subs.append(sub_id)
            else:
                needs_consent_subs.append(sub_id)

        # A consent-requiring toggle forks an OAuth flow whose signed state
        # carries this origin, so fail closed on an off-list Origin here — after
        # the pure consent/inline split, before the inline persist below — so a
        # spoofed Origin never commits a partial sub-service change. An
        # inline-only toggle has no redirect flow and is deliberately not gated.
        if needs_consent_subs:
            redirect.validate_origin_allowed(origin)

        new_enabled = sorted((current - to_remove) | set(can_inline_subs))
        record.enabled_sub_services = new_enabled
        # Compare-and-set against the ciphertext this operation loaded: the
        # lock is best-effort, so a peer (e.g. a token refresh that rotated the
        # refresh token) may have written meanwhile — losing the CAS raises
        # rather than clobbering the peer's record.
        await _persist(record, expected_blob=started_blob)

        # Reconcile the inline manifest changes in ONE pipeline transaction
        # (remove + append in a single mutator ⇒ one persist, one reload, one
        # broadcast) INSIDE the lock so a concurrent disconnect (which also takes
        # the lock) cannot delete the connection between this persist and the
        # reconcile and strand the added entries against a deleted connection.
        removed: list[str] = []
        added: list[str] = []

        def reconcile(document: dict[str, Any]) -> None:
            removed[:] = (
                manifest_writer.remove_managed_entries(document, connection_id=connection_id, sub_services=to_remove)
                if to_remove
                else []
            )
            added[:] = (
                manifest_writer.add_managed_entries(
                    document,
                    descriptor=descriptor,
                    enabled_sub_services=can_inline_subs,
                    alias=record.alias,
                    connection_id=connection_id,
                )
                if can_inline_subs
                else []
            )

        # A consent-only toggle (nothing to remove, nothing inline-addable) makes no
        # manifest change here, so no apply_change runs and the fanout is honestly
        # ``None``; the forked consent flow's own completion reports its broadcast.
        fanout: dict[str, Any] | None = None
        if to_remove or can_inline_subs:
            fanout = (await _svc.ConfigService.from_app().apply_change(reconcile)).fanout

    if not needs_consent_subs:
        return PatchResult(
            connection_id=connection_id,
            enabled_sub_services=new_enabled,
            consent_required=False,
            flow_id=None,
            authorize_url=None,
            added_manifest_entries=added,
            removed_manifest_entries=removed,
            fanout=fanout,
        )

    # Consent fork: carry both already-enabled and newly-requested sub-services
    # so the callback produces a complete record.
    consent_subs = sorted(set(new_enabled) | set(needs_consent_subs))
    result = await _start_flow(
        descriptor=descriptor,
        alias=record.alias,
        enabled_sub_services=consent_subs,
        requested_scopes=_scopes_for(descriptor, consent_subs),
        return_url=return_url,
        redirect_uri=redirect_uri,
        origin=origin,
        operation=state.FlowOperation.TOGGLE_SUBSERVICE_ON,
        reconnect_connection_id=connection_id,
    )
    return PatchResult(
        connection_id=connection_id,
        enabled_sub_services=new_enabled,
        consent_required=True,
        flow_id=result.flow_id,
        authorize_url=result.authorize_url,
        added_manifest_entries=added,
        removed_manifest_entries=removed,
        fanout=fanout,
    )
