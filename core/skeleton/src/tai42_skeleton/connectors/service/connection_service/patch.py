"""Toggle which sub-services are enabled on a connection.

Remove/add inline under the lock, or fork a consent OAuth flow when a newly-enabled sub-service needs
new scopes.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.connectors.models import ConnectionRecord
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.service import PatchResult

from tai42_skeleton.connectors.oauth import redirect, state
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.service import manifest_writer

from .flow import _start_flow
from .persist import _persist
from .validation import _validate_return_url, _validate_sub_services


def _split_toggled_on(
    record: ConnectionRecord, descriptor: ProviderDescriptor, to_add: set[str]
) -> tuple[list[str], list[str]]:
    """Split the sub-services toggled ON into those recreated inline and those that need consent.

    A no-auth connection has no OAuth endpoints, so there is no consent flow to fork — every toggle
    is inline regardless of any scopes the descriptor declares.
    """
    granted = set(record.granted_scopes)
    can_inline: list[str] = []
    needs_consent: list[str] = []
    for sub_id in to_add:
        if record.kind == "none" or set(descriptor.sub_services[sub_id].scopes).issubset(granted):
            can_inline.append(sub_id)
        else:
            needs_consent.append(sub_id)
    return can_inline, needs_consent


async def patch_sub_services(
    *,
    connection_id: str,
    desired: list[str],
    return_url: str | None,
    redirect_uri: str,
    origin: str,
) -> PatchResult:
    """Toggle which sub-services are enabled.

    Sub-services toggled OFF lose their manifest entries. Sub-services toggled
    ON whose scopes are already granted are recreated inline; otherwise a
    ``TOGGLE_SUBSERVICE_ON`` flow is started and the authorize URL returned for a
    consent popup that :func:`~tai42_skeleton.connectors.service.connection_service.complete.complete_connect`
    finalises. The connection's whole enabled set is reconciled, so an owned entry the run-time check
    refuses (it fell behind its descriptor) is rewritten; an unchanged set is accepted only to do that.
    """
    landing = _validate_return_url(return_url)

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
        config_service = _svc.ConfigService.from_app()
        if current == desired_set:
            # Saving the services again is how a connection that cannot reconnect (no-auth) has its
            # entries brought back to what the descriptor defines: an unchanged set is refused only
            # when no owned entry would be rewritten.
            pending = manifest_writer.managed_entries_to_write(
                config_service.read_manifest_document(),
                descriptor=descriptor,
                enabled_sub_services=sorted(current),
                alias=record.alias,
                connection_id=connection_id,
            )
            if not pending.rewritten:
                raise ValueError("enabled_sub_services unchanged")

        to_remove = current - desired_set
        to_add = desired_set - current

        can_inline_subs, needs_consent_subs = _split_toggled_on(record, descriptor, to_add)

        # A consent-requiring toggle forks an OAuth flow whose signed state
        # carries this origin, so fail closed on an off-list Origin here — after
        # the pure consent/inline split, before the inline persist below — so a
        # spoofed Origin never commits a partial sub-service change. An
        # inline-only toggle has no redirect flow and is deliberately not gated.
        if needs_consent_subs:
            redirect.validate_origin_allowed(origin)

        new_enabled = sorted((current - to_remove) | set(can_inline_subs))
        if new_enabled != sorted(current):
            record.enabled_sub_services = new_enabled
            # Compare-and-set against the ciphertext this operation loaded: the
            # lock is best-effort, so a peer (e.g. a token refresh that rotated the
            # refresh token) may have written meanwhile — losing the CAS raises
            # rather than clobbering the peer's record.
            await _persist(record, expected_blob=started_blob)

        # Reconcile the manifest in ONE pipeline transaction (remove + append/rewrite in a
        # single mutator ⇒ one persist, one reload, one broadcast) INSIDE the lock so a
        # concurrent disconnect (which also takes the lock) cannot delete the connection
        # between this persist and the reconcile and strand the written entries. The whole
        # enabled set is reconciled, so an owned entry that fell behind the descriptor is
        # rewritten too.
        removed: list[str] = []
        added: list[str] = []

        def reconcile(document: dict[str, Any]) -> None:
            removed[:] = (
                manifest_writer.remove_managed_entries(document, connection_id=connection_id, sub_services=to_remove)
                if to_remove
                else []
            )
            added[:] = manifest_writer.add_managed_entries(
                document,
                descriptor=descriptor,
                enabled_sub_services=new_enabled,
                alias=record.alias,
                connection_id=connection_id,
            )

        # The pipeline runs only when the manifest changes; a consent-only toggle changes
        # nothing here, so the fanout is honestly ``None`` and the forked consent flow's own
        # completion reports its broadcast.
        pending_write = manifest_writer.managed_entries_to_write(
            config_service.read_manifest_document(),
            descriptor=descriptor,
            enabled_sub_services=new_enabled,
            alias=record.alias,
            connection_id=connection_id,
        )
        applied = await config_service.apply_change(reconcile) if to_remove or pending_write else None

    fanout = None if applied is None else applied.fanout
    span_release_error = None if applied is None else applied.span_release_error

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
            span_release_error=span_release_error,
        )

    # Consent fork: carry both already-enabled and newly-requested sub-services
    # so the callback produces a complete record.
    consent_subs = sorted(set(new_enabled) | set(needs_consent_subs))
    result = await _start_flow(
        descriptor=descriptor,
        alias=record.alias,
        enabled_sub_services=consent_subs,
        return_url=landing,
        redirect_uri=redirect_uri,
        origin=origin,
        operation=state.FlowOperation.TOGGLE_SUBSERVICE_ON,
        reconnect_connection_id=connection_id,
        record=record,
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
        span_release_error=span_release_error,
    )
