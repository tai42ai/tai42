"""Reverse a failed install or update in the order that keeps the fleet consistent.

Reverts the env write, reverses the manifest patch, and re-instates the package or old pin.

The manifest patch is reversed through the pipeline from the stored manifest the write
exclusion holds (:func:`~tai42_skeleton.marketplace.manifest_patch.revert_provides`), so
every edit another config door landed meanwhile stands. The env write is reverted AFTER
the manifest reversal so no restored marker references a key being dropped; the old wheel
goes back BEFORE the reversal's reload so the old manifest never loads against the new
wheel. Any failed sub-step escalates to :class:`InstallUnwindError`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_skeleton.config.service import ApplyResult, ConfigService, joined_span_release_errors
from tai42_skeleton.marketplace import env_apply, package_ops
from tai42_skeleton.marketplace.errors import InstallUnwindError
from tai42_skeleton.marketplace.manifest_patch import ProvidesUndo, revert_provides
from tai42_skeleton.marketplace.pip import PipRunner
from tai42_skeleton.marketplace.prefix import environment_constraints
from tai42_skeleton.marketplace.store import InstallRecord
from tai42_skeleton.operations._broadcast import FleetBroadcastError


async def unwind_install(
    step_error: Exception,
    *,
    package: str | None,
    provides_undo: ProvidesUndo | None,
    env_written: env_apply.EnvWrite | None,
    svc: ConfigService,
    cm: Any,
    pip_runner: PipRunner,
    prefix: str | None,
    prefix_uninstall: Callable[[str, str], None],
    prefix_has_dist: Callable[[str, str], bool],
    env_dist_version: Callable[[str, str], str | None],
) -> None:
    """Reverse an install whose Step 4/5 failed.

    Reverses the provides patch through the pipeline (converging the live app and the fleet
    back) when the change had persisted — ``provides_undo`` is that patch's
    :class:`ProvidesUndo`, ``None`` when nothing persisted — then reverts THIS install's env
    write ``env_written`` (the combined pipeline leaves it standing as an orphaned,
    unreferenced write; :func:`~tai42_skeleton.marketplace.env_apply.revert_env_write`
    reverts exactly this install's contribution against the stored env), then pip
    uninstalls the freshly-installed package. A descriptor-only spec (``package is None``)
    installed nothing, so no package is removed. A failed sub-step escalates to
    :class:`InstallUnwindError`; otherwise the caller re-raises the original step error.
    """
    try:
        if provides_undo is not None:
            await _revert_manifest(svc, provides_undo)
        # Revert AFTER the manifest reversal so no restored marker references a
        # key being dropped (on the orphan path the manifest never persisted, so
        # the live manifest already names none of these keys).
        await env_apply.revert_env_write(svc, cm, env_written)
    except Exception as unwind_error:
        raise InstallUnwindError(step_error, unwind_error) from step_error
    if package is None:
        # Nothing was pip-installed for a descriptor-only plugin — no package to remove.
        return
    try:
        await package_ops.remove_package(
            package,
            prefix=prefix,
            pip_runner=pip_runner,
            prefix_uninstall=prefix_uninstall,
            prefix_has_dist=prefix_has_dist,
            env_dist_version=env_dist_version,
        )
    except Exception as unwind_error:
        raise InstallUnwindError(step_error, unwind_error) from step_error


async def unwind_update(
    step_error: Exception,
    *,
    old_package: str | None,
    new_package: str | None,
    row: InstallRecord,
    provides_undo: ProvidesUndo | None,
    env_written: env_apply.EnvWrite | None,
    svc: ConfigService,
    cm: Any,
    pip_runner: PipRunner,
    prefix: str | None,
    prefix_uninstall: Callable[[str, str], None],
    env_constraints: Callable[[str], list[str]] = environment_constraints,
) -> str | None:
    """Reverse an update whose Step 5/6 failed.

    Reinstalls the OLD pin, reverses the provides swap through the pipeline when the change
    had persisted (``provides_undo`` is the swap's :class:`ProvidesUndo`, ``None`` when
    nothing persisted), then reverts THIS update's env write ``env_written`` against the
    stored env (:func:`~tai42_skeleton.marketplace.env_apply.revert_env_write`). The old
    wheel goes back BEFORE the reversal's reload so the old manifest never loads against the
    new wheel. A github old pin is reinstalled through the SAME fetch-and-verify path as a
    forward install, using the stored row's ``artifact_ref`` + ``sha256`` — the old
    version's integrity is re-checked, never cloned from a mutable tag. On the prefix path
    the NEW version's files are removed first, or the old and new dist-info would coexist
    in the prefix. A descriptor-only update (``old_package``/``new_package`` both None)
    installed no package, so only the manifest and env write are reverted. A failed
    sub-step (including an integrity mismatch on the old artifact) escalates to
    :class:`InstallUnwindError`.

    Returns the failed span releases of the config writes the unwind landed (the manifest
    reversal, then the env revert), joined in write order — ``None`` when each released or
    nothing was written. An escalated :class:`InstallUnwindError` carries the same for the
    writes landed before the failure, a failing write's :class:`FleetBroadcastError` (landed,
    then its propagation failed) included.
    """
    released: list[str | None] = []
    try:
        if old_package is not None:
            # A packaged update: put the OLD wheel back BEFORE the manifest reversal's
            # reload. The delivery-form check guarantees new_package is packaged too.
            if prefix is not None:
                if new_package is None:
                    raise AssertionError  # noqa: TRY301 type-narrowing guard; wrapped as InstallUnwindError below
                prefix_uninstall(new_package, prefix)
            await package_ops.pip_install(
                pip_runner,
                prefix,
                old_package,
                row.version,
                row.source,
                row.artifact_ref,
                row.sha256,
                env_constraints=env_constraints,
            )
        if provides_undo is not None:
            released.append((await _revert_manifest(svc, provides_undo)).span_release_error)
        # Revert the env write this update made AFTER the old manifest is back, so
        # the restored old entry never references a dropped key.
        released.append(await env_apply.revert_env_write(svc, cm, env_written))
    except Exception as unwind_error:
        if isinstance(unwind_error, FleetBroadcastError):
            released.append(unwind_error.span_release_error)
        raise InstallUnwindError(
            step_error, unwind_error, span_release_error=joined_span_release_errors(*released)
        ) from step_error
    return joined_span_release_errors(*released)


async def _revert_manifest(svc: ConfigService, undo: ProvidesUndo) -> ApplyResult:
    """Reverse one provides patch on the stored manifest through the pipeline."""

    def mutator(document: dict[str, Any]) -> None:
        revert_provides(document, undo)

    return await svc.apply_change(mutator)
