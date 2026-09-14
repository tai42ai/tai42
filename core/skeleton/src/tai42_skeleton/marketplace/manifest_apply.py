"""Mutate the persisted manifest through :class:`ConfigService` and shape the
reload/fanout report.

Every manifest edit crosses the config-mutation pipeline: it validates the RESOLVED
projection of the mutated document (``!ENV`` markers materialized), persists,
reloads locally through the reload gate, and broadcasts the confirmed change on the
worker bus — so a compose the resolved schema or the backend-needs-bus invariant
rejects raises loudly inside the transaction instead of corrupting the stored
manifest, mapped here to the typed :class:`ManifestComposeError`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.config.service import ApplyResult, ConfigService
from tai42_skeleton.marketplace.errors import ManifestComposeError
from tai42_skeleton.operations._broadcast import fleet_fanout

logger = logging.getLogger(__name__)


def reload_report(result: ApplyResult) -> dict[str, Any]:
    """The manifest apply's local reload result with the standard fleet fan-out
    summary folded in under ``fanout`` — the ``reload`` field every
    install/uninstall/update response carries. Reuses the shared
    :func:`~tai42_skeleton.operations._broadcast.fleet_fanout` shaper so the key AND the
    value shape match every other fleet-report-embedding writer (backup, the
    operations ``apply_response``)."""
    return {**result.local, "fanout": fleet_fanout(result.fleet)}


async def apply_composed(svc: ConfigService, mutator: Callable[[dict[str, Any]], None]) -> ApplyResult:
    """Apply a manifest mutation through the pipeline, mapping a composed-manifest
    fault to the typed compose error.

    The pipeline validates the RESOLVED projection of the mutated document (``!ENV``
    markers materialized) before it persists, so a marker on a non-string field
    (e.g. ``api_tools.expose_destructive``) validates against its resolved value —
    the marketplace does no schema validation of its own. Because the marketplace
    fully controls the composed document, a resolved-projection schema failure
    (:class:`~pydantic.ValidationError`) OR a registered-backend-without-a-bus refusal
    (:class:`~tai42_skeleton.app.boot_rules.BackendNeedsBusError`, which the whole-fleet
    boundary would otherwise let escape untyped) is a registry-spec + local fault,
    re-raised as :class:`ManifestComposeError` so the boundary attributes it a loud
    500 rather than a bare one. Either raises inside the transaction, so nothing
    persists. A resolved-secret leak (a ``ResolvedSecretError``) cannot arise here:
    the structural provides patch ingests no resolved secret value, so the pipeline's
    secret seal is a no-op."""
    try:
        return await svc.apply_change(mutator)
    except (ValidationError, BackendNeedsBusError) as exc:
        raise ManifestComposeError(f"the composed manifest is invalid: {exc}") from exc


async def remount_reload(
    svc: ConfigService, read_manifest: Callable[[], dict[str, Any]], prior: ApplyResult
) -> ApplyResult:
    """Re-reload AFTER the attribution row is persisted so the mount map re-reads the
    store and (re)mounts route-carrying items at their PERSISTED base — the remap — in
    THIS process, with no reboot.

    The forward provides reload runs BEFORE the attribution row exists (the record is
    the last committing step, so the unwind can restore the manifest when a record
    fails), so the mount map (:meth:`AppLifecycle._installed_route_mounts`, which reads
    ``MarketplaceInstallStore.list_installed()``) sees no persisted ``route_mounts`` and
    mounts every route at its DECLARED base. This second reload — issued once the row is
    in place — rebuilds the serving surface with the persisted mounts live, so an
    operator remap serves immediately instead of only after the next reboot. It
    re-applies the ALREADY-persisted manifest (an identity replace) purely to drive the
    reload; no manifest content changes.

    Best-effort by design: the install/update is already committed and internally
    consistent (package + manifest + row all agree), so a failed remount degrades to
    the pre-fix behavior — the routes serve at their declared base until the next reboot
    re-reads the store — and MUST NOT unwind a good install. The failure is logged; the
    forward reload's ``prior`` result stands in for the receipt. ``read_manifest`` is the
    caller's persisted-manifest read, kept a callable so the best-effort read still
    happens inside the guarded body. Returns the remount's result on success, else
    ``prior``."""
    try:
        return await svc.apply_replace(read_manifest())
    except Exception:
        logger.warning(
            "marketplace: post-record remount reload failed; persisted route mounts will take effect "
            "on the next reboot",
            exc_info=True,
        )
        return prior
