"""The env-store side of a provides change.

The pre-check, the combined env+manifest apply, and the exact-restore revert.
A spec that ACCEPTS env (an ``!ENV`` marker or a required connector env) writes its
supplied values in the SAME pipeline unit as its provides entry, so the entry lands
exactly once alongside the values. The unwind restores every key this operation
wrote to its captured PRIOR store value, so an overwritten operator key is never
blind-deleted and the secret-marks var keeps an operator's other marks.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from tai42_contract.plugins import PluginSpec
from tai42_kit.plugins import accepts_env, required_env_for_spec

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.config.service import ApplyResult, ConfigService
from tai42_skeleton.marketplace.errors import InstallEnvError, InstallStateError, ManifestComposeError
from tai42_skeleton.marketplace.manifest_apply import apply_composed
from tai42_skeleton.marketplace.provides import env_manifest_pointer

# The operator's "treat these env keys as secret" marks (comma-separated key names),
# the same var ``set_mcp_secret_env`` appends to so the Studio editor masks the value.
SECRET_MARKS_VAR = "TAI_ENV_SECRET_KEYS"  # noqa: S105 constant identifier, not a secret value


def precheck_required_env(cm: Any, spec: PluginSpec, env: dict[str, str] | None) -> None:
    """Refuse an install/update whose required env is not supplied, stored, or in the process env.

    A readable failure BEFORE any pip work, naming the missing vars. The authoritative refusal is
    the config pipeline's :func:`~tai42_skeleton.config.boundary.refuse_unresolved_env` at the
    manifest write; this is only the early, named one so an operator sees the gap
    before a minutes-long install begins.
    """
    required = required_env_for_spec(spec)
    if not required:
        return
    supplied = set(env or {})
    try:
        stored = set(cm.read_env())
    except FileNotFoundError:
        stored = set()
    available = supplied | stored | set(os.environ)
    missing = [req.name for req in required if req.name not in available]
    if missing:
        raise InstallStateError(
            f"{spec.ref} requires env var(s) not supplied, stored, or present in the process "
            f"environment: {', '.join(missing)}"
        )


def env_to_write(env: dict[str, str] | None) -> dict[str, str]:
    """The subset of ``env`` this install actually writes to the store.

    Keys the process env does not already provide. A deployment/already-set var is omitted
    (its marker resolves from the live env), so no pre-existing marker can end up
    referencing a key this install owns.
    """
    return {key: value for key, value in (env or {}).items() if key not in os.environ}


async def revert_env_write(svc: ConfigService, cm: Any, env_restore: dict[str, str] | None) -> None:
    """Revert THIS install's env contribution through the ConfigService env door.

    Restores every key this install wrote to the PRIOR store value ``prepare``
    captured (its exact prior string, or ``""`` to delete a key that was ABSENT
    before) — never a blind delete, so a key this install merely OVERWROTE keeps its
    operator value and the ``TAI_ENV_SECRET_KEYS`` marks var keeps an operator's OTHER
    marks. The change set is DIFFED against the live store, so when nothing actually
    persisted (the pre-persist dangling refusal) it is EMPTY and no env change fires —
    no fleet broadcast on a no-op. Runs AFTER any manifest restore, so no restored
    marker references a dropped key.
    """
    if not env_restore:
        return
    try:
        current = cm.read_env()
    except FileNotFoundError:
        current = {}
    changes = {key: value for key, value in env_restore.items() if current.get(key, "") != value}
    if changes:
        await svc.apply_env_change(changes)


async def apply_provides_change(
    svc: ConfigService,
    spec: PluginSpec,
    mutator: Callable[[dict[str, Any]], None],
    *,
    env: dict[str, str] | None,
    secret_keys: list[str] | None,
    env_to_write: dict[str, str],
    env_restore: dict[str, str],
) -> ApplyResult:
    """Persist a provides patch through the pipeline.

    A spec that ACCEPTS env (:func:`~tai42_kit.plugins.accepts_env` — any ``!ENV``
    marker or a non-empty required-env, i.e. an mcp-server with markers or an oauth
    connector) routes through the COMBINED env+manifest pipeline
    (:meth:`~tai42_skeleton.config.service.ConfigService.apply_env_and_change`): the
    supplied env values land in the store and the provides entry is written in ONE
    unit, so the entry is written exactly once (the standalone ``apply_provides``
    never runs a second time). ``manifest_pointer`` is the binding field of the
    spec's data items (``mcp`` / ``connectors``), named in the orphan report. An
    unsatisfied required marker or connector-env is the pipeline's unresolved-env
    refusal, surfaced as :class:`InstallEnvError` naming each missing var +
    json-pointer BEFORE anything persists. The non-atomicity contract is preserved (a
    manifest-persist failure leaves the env write standing and raises
    ``OrphanEnvWriteError``); the installer-unwind above reverts it.

    Under the pipeline's env-write lock (against the SAME stored-env snapshot the write
    derives from) this records the PRIOR store value of EVERY key it will write into
    ``env_restore`` — the value keys AND, when ``secret_keys`` are supplied, the
    ``TAI_ENV_SECRET_KEYS`` marks var it MERGES into (each key's exact prior string, or
    ``""`` when the key was absent). The unwind restores exactly those, so it reverts
    this install's contribution without deleting an operator's pre-existing store value
    or clobbering their other secret marks.

    A spec that accepts no env routes through ``apply_change``; ``env`` /
    ``secret_keys`` are meaningless there and supplying them is a loud input error.
    """
    if not accepts_env(spec):
        if env or secret_keys:
            raise InstallEnvError(
                f"env / secret_keys were supplied but {spec.ref} declares no install-time env "
                "(no !ENV marker and no required connector env)"
            )
        return await apply_composed(svc, mutator)

    marks = list(secret_keys or [])
    manifest_pointer = env_manifest_pointer(spec)

    async def prepare(stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        changes = dict(env_to_write)
        if marks:
            # Append to the marks read from the STORED env (never the settings
            # cache, stale until a reload), mirroring ``set_mcp_secret_env``.
            prior = stored.get(SECRET_MARKS_VAR)
            existing = [m.strip() for m in (prior or "").split(",") if m.strip()]
            changes[SECRET_MARKS_VAR] = ",".join(dict.fromkeys([*existing, *marks]))
        # Capture the PRIOR store value of EVERY key this install writes (value keys
        # AND the marks var) from the STORED snapshot: its exact prior string when
        # present, else ``""`` (the key was absent). The unwind restores each to this,
        # so an overwritten pre-existing store key is never blind-deleted and the marks
        # var keeps an operator's OTHER marks.
        for key in changes:
            env_restore[key] = stored.get(key, "")
        return changes, mutator

    try:
        return await svc.apply_env_and_change(prepare, manifest_pointer=manifest_pointer)
    except (ValidationError, BackendNeedsBusError) as exc:
        raise ManifestComposeError(f"the composed manifest is invalid: {exc}") from exc
    except ValueError as exc:
        # A dangling-marker refusal (each missing required var + json-pointer) or
        # another env-boundary refusal — raised before anything persisted.
        raise InstallEnvError(str(exc)) from exc
