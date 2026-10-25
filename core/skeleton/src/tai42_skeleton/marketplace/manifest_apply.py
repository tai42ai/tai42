"""Mutate the persisted manifest through :class:`ConfigService` and shape the reload/fanout report.

Every manifest edit crosses the config-mutation pipeline: it validates the RESOLVED
projection of the mutated document (``!ENV`` markers materialized), persists,
reloads locally through the reload gate, and broadcasts the confirmed change on the
worker bus — so a compose the resolved schema or the backend-needs-bus invariant
rejects raises loudly inside the transaction instead of corrupting the stored
manifest, mapped here to the typed :class:`ManifestComposeError`; a boundary refusal of
the composed document maps to the typed :class:`ManifestRefusedError`.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from tai42_skeleton.app.boot_rules import translate_config_invariants
from tai42_skeleton.config.service import ApplyResult, ConfigService, joined_span_release_errors
from tai42_skeleton.marketplace.errors import ManifestComposeError, ManifestRefusedError
from tai42_skeleton.operations._broadcast import FleetBroadcastError

logger = logging.getLogger(__name__)


def composed_manifest_invalid(detail: str) -> ManifestComposeError:
    """The typed fault for a composed manifest the pipeline refuses."""
    return ManifestComposeError(f"the composed manifest is invalid: {detail}")


async def apply_composed(svc: ConfigService, mutator: Callable[[dict[str, Any]], None]) -> ApplyResult:
    """Apply a manifest mutation through the pipeline, mapping a composed-manifest fault to the typed compose error.

    The pipeline validates the RESOLVED projection of the mutated document (``!ENV``
    markers materialized) before it persists, so a marker on a non-string field
    (e.g. ``api_tools.expose_destructive``) validates against its resolved value —
    the marketplace does no schema validation of its own. Because the marketplace
    fully controls the composed document, a resolved-projection schema failure
    (:class:`~pydantic.ValidationError`) OR a registered-backend-without-a-bus refusal
    (:class:`~tai42_skeleton.app.boot_rules.BackendNeedsBusError`, which the whole-fleet
    boundary would otherwise let escape untyped) is a registry-spec + local fault,
    re-raised as :class:`ManifestComposeError` so the boundary attributes it a loud
    500 rather than a bare one. A boundary refusal of the composed document (a plain
    :class:`ValueError`: a tool-extension combo whose dry-run bind refuses, named by its
    manifest path, or a dangling ``!ENV`` marker) reflects the deployment's own manifest
    state, not the registry spec, so it is re-raised as :class:`ManifestRefusedError` (a
    400) with its text kept. Each raises inside the transaction, so nothing persists. A
    resolved-secret leak (a ``ResolvedSecretError``) cannot arise here:
    the structural provides patch ingests no resolved secret value, so the pipeline's
    secret seal is a no-op.
    """
    with translate_config_invariants(composed_manifest_invalid):
        try:
            return await svc.apply_change(mutator)
        except ValidationError as exc:
            raise composed_manifest_invalid(str(exc)) from exc
        except ValueError as exc:
            raise ManifestRefusedError(str(exc)) from exc


async def remount_reload(svc: ConfigService, prior: ApplyResult) -> ApplyResult:
    """Re-reload AFTER the attribution row is persisted so the mount map re-reads the store.

    (Re)mounts route-carrying items at their PERSISTED base — the remap — in THIS process, with no reboot.

    The forward provides reload runs BEFORE the attribution row exists (the record is
    the last committing step, so the unwind can reverse the provides patch when a record
    fails), so the mount map (:meth:`AppLifecycle._installed_route_mounts`, which reads
    ``MarketplaceInstallStore.list_installed()``) sees no persisted ``route_mounts`` and
    mounts every route at its DECLARED base. This second reload — issued once the row is
    in place — rebuilds the serving surface with the persisted mounts live, so an
    operator remap serves immediately instead of only after the next reboot. It
    persists the stored manifest unchanged, read under the write exclusion
    (:meth:`~tai42_skeleton.config.service.ConfigService.reapply_manifest`), purely to drive
    the reload; no manifest content changes, and a write another door landed meanwhile stands.

    Best-effort by design: the install/update is already committed and internally
    consistent (package + manifest + row all agree), so a failed remount leaves the routes
    serving at their declared base until the next reboot re-reads the store, and MUST NOT
    unwind a good install. The failure is logged; the forward reload's ``prior`` result
    stands in for the receipt. The remount is the same change continued: it hands the
    pipeline ``prior`` — the change's already-run roll — so the remount pins and rolls nothing
    and judges its reload with that roll (one verdict). Returns the remount's result on
    success, else ``prior`` — carrying, beside its own, the remount's failed span release when
    the remount's write landed and its propagation raised :class:`FleetBroadcastError`.
    """
    try:
        return await svc.reapply_manifest(continues=prior)
    except Exception as exc:
        logger.warning(
            "marketplace: post-record remount reload failed; persisted route mounts will take effect "
            "on the next reboot",
            exc_info=True,
        )
        if isinstance(exc, FleetBroadcastError) and exc.span_release_error is not None:
            return dataclasses.replace(
                prior, span_release_error=joined_span_release_errors(prior.span_release_error, exc.span_release_error)
            )
        return prior
