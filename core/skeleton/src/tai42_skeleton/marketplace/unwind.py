"""Reverse a failed install, update or uninstall in the order that keeps the fleet consistent.

The plugin environments go back first: each ``current`` returns to the build that was in
use before the operation and the new build is deleted, with no pip call
(:meth:`~tai42_skeleton.marketplace.environments.change.OperationChange.swap_back`), so the
manifest reversal's reload imports against the environment the restored manifest was
written for. The manifest patch is then reversed through the pipeline from the stored
manifest the write exclusion holds
(:func:`~tai42_skeleton.marketplace.manifest_patch.revert_provides`), so every edit another
config door landed meanwhile stands, and the env write is reverted last so no restored
marker references a key being dropped. Any failed sub-step escalates to
:class:`InstallUnwindError`.
"""

from __future__ import annotations

from typing import Any

from tai42_skeleton.config.service import ApplyResult, ConfigService, joined_span_release_errors
from tai42_skeleton.marketplace import env_apply
from tai42_skeleton.marketplace.environments.change import OperationChange
from tai42_skeleton.marketplace.errors import InstallUnwindError
from tai42_skeleton.marketplace.manifest_patch import ProvidesUndo, revert_provides
from tai42_skeleton.operations._broadcast import FleetBroadcastError

ENVIRONMENT_RESTORED = "the previous plugin environment was restored"


async def unwind_operation(
    step_error: BaseException,
    *,
    change: OperationChange | None,
    provides_undo: ProvidesUndo | None,
    env_written: env_apply.EnvWrite | None,
    svc: ConfigService,
    cm: Any,
) -> str | None:
    """Reverse an operation whose step after the environment swap failed or was cancelled.

    Swaps the environments ``change`` made back (``None`` when the operation changed no
    environment), reverses the provides patch when it had persisted (``provides_undo`` is
    that patch's :class:`ProvidesUndo`, ``None`` when nothing persisted), then reverts the
    operation's env write ``env_written`` against the stored env
    (:func:`~tai42_skeleton.marketplace.env_apply.revert_env_write`). A restored environment
    adds :data:`ENVIRONMENT_RESTORED` as a note on ``step_error``, which the caller re-raises.

    Returns the failed span releases of the config writes the unwind landed (the manifest
    reversal, then the env revert), joined in write order — ``None`` when each released or
    nothing was written. An escalated :class:`InstallUnwindError` carries the same for the
    writes landed before the failure, a failing write's :class:`FleetBroadcastError` (landed,
    then its propagation failed) included.
    """
    released: list[str | None] = []
    try:
        if change is not None:
            change.swap_back()
        if provides_undo is not None:
            released.append((await _revert_manifest(svc, provides_undo)).span_release_error)
        released.append(await env_apply.revert_env_write(svc, cm, env_written))
    except Exception as unwind_error:
        if isinstance(unwind_error, FleetBroadcastError):
            released.append(unwind_error.span_release_error)
        raise InstallUnwindError(
            step_error, unwind_error, span_release_error=joined_span_release_errors(*released)
        ) from step_error
    if change is not None and change.restores_builds:
        step_error.add_note(ENVIRONMENT_RESTORED)
    return joined_span_release_errors(*released)


async def _revert_manifest(svc: ConfigService, undo: ProvidesUndo) -> ApplyResult:
    """Reverse one provides patch on the stored manifest through the pipeline."""

    def mutator(document: dict[str, Any]) -> None:
        revert_provides(document, undo)

    return await svc.apply_change(mutator)
