"""Reverse a failed install or update — revert the env write, restore the manifest,
re-instate the package or old pin — in the order that keeps the fleet consistent.

The env write is reverted AFTER the manifest restore so no restored marker
references a key being dropped; the old wheel goes back BEFORE the manifest
restore's reload so the old manifest never loads against the new wheel. Any failed
sub-step escalates to :class:`InstallUnwindError`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.marketplace import env_apply, package_ops
from tai42_skeleton.marketplace.errors import InstallUnwindError
from tai42_skeleton.marketplace.pip import PipRunner
from tai42_skeleton.marketplace.store import InstallRecord


async def unwind_install(
    step_error: Exception,
    *,
    package: str | None,
    saved_manifest: dict[str, Any] | None,
    env_restore: dict[str, str] | None,
    svc: ConfigService,
    cm: Any,
    pip_runner: PipRunner,
    prefix: str | None,
    prefix_uninstall: Callable[[str, str], None],
    prefix_has_dist: Callable[[str, str], bool],
    env_dist_version: Callable[[str, str], str | None],
) -> None:
    """Reverse an install whose Step 4/5 failed: revert THIS install's env write
    (the combined pipeline leaves it standing as an inert orphan, but this layer
    restores exactly this install's contribution — each key it wrote back to its
    captured PRIOR store value, so a newly-created key is deleted, an overwritten
    pre-existing key keeps its operator value, and the ``TAI_ENV_SECRET_KEYS`` marks
    var keeps an operator's OTHER marks), restore the manifest through the pipeline
    (converging the live app and the fleet back) when the change had persisted, then
    pip uninstall the freshly-installed package. A descriptor-only spec
    (``package is None``) installed nothing, so no package is removed. A failed
    sub-step escalates to :class:`InstallUnwindError`; otherwise the caller re-raises
    the original step error."""
    try:
        if saved_manifest is not None:
            await svc.apply_replace(saved_manifest)
        # Revert AFTER the manifest restore so no restored marker references a
        # key being dropped (on the orphan path the manifest never persisted, so
        # the live manifest already names none of these keys).
        await env_apply.revert_env_write(svc, cm, env_restore)
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
    saved_manifest: dict[str, Any] | None,
    env_restore: dict[str, str] | None,
    svc: ConfigService,
    cm: Any,
    pip_runner: PipRunner,
    prefix: str | None,
    prefix_uninstall: Callable[[str, str], None],
) -> None:
    """Reverse an update whose Step 5/6 failed: revert THIS update's env write (every
    key it wrote restored to its captured PRIOR store value — a newly-created key
    deleted, an overwritten one kept, the ``TAI_ENV_SECRET_KEYS`` marks var back to its
    prior value), reinstall the OLD pin, then restore the manifest through the
    pipeline (when the change had persisted).
    The old wheel goes back BEFORE the restore's reload so the old manifest never
    loads against the new wheel. A github old pin is reinstalled through the SAME
    fetch-and-verify path as a forward install, using the stored row's
    ``artifact_ref`` + ``sha256`` — the old version's integrity is re-checked,
    never cloned from a mutable tag. On the prefix path the NEW version's files
    are removed first, or the old and new dist-info would coexist in the prefix.
    A descriptor-only update (``old_package``/``new_package`` both None) installed no
    package, so only the manifest and env write are reverted. A failed sub-step
    (including an integrity mismatch on the old artifact) escalates to
    :class:`InstallUnwindError`."""
    try:
        if old_package is not None:
            # A packaged update: put the OLD wheel back BEFORE the manifest restore's
            # reload. The delivery-form check guarantees new_package is packaged too.
            if prefix is not None:
                assert new_package is not None
                prefix_uninstall(new_package, prefix)
            await package_ops.pip_install(
                pip_runner, prefix, old_package, row.version, row.source, row.artifact_ref, row.sha256
            )
        if saved_manifest is not None:
            await svc.apply_replace(saved_manifest)
        # Revert the env write this update made AFTER the old manifest is back, so
        # the restored old entry never references a dropped key.
        await env_apply.revert_env_write(svc, cm, env_restore)
    except Exception as unwind_error:
        raise InstallUnwindError(step_error, unwind_error) from step_error
