"""The env-store side of a provides change.

The pre-check, the combined env+manifest apply, and the revert of exactly that write.
A spec that ACCEPTS env (an ``!ENV`` marker or a required connector env) writes its
supplied values in the SAME pipeline unit as its provides entry, so the entry lands
exactly once alongside the values. The unwind derives its revert from the stored env
under the write exclusion: each value key this operation wrote goes back to its PRIOR
store value while the store still holds the value written, and only the secret marks
this operation added leave the marks var, so every other write stands.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from tai42_contract.plugins import PluginSpec
from tai42_kit.plugins import accepts_env, required_env_for_spec

from tai42_skeleton.app.boot_rules import translate_config_invariants
from tai42_skeleton.config.service import ApplyResult, ConfigService
from tai42_skeleton.marketplace.errors import EnvRevertConflictError, InstallEnvError, InstallStateError
from tai42_skeleton.marketplace.manifest_apply import apply_composed, composed_manifest_invalid
from tai42_skeleton.marketplace.provides import env_manifest_pointer
from tai42_skeleton.settings.env_secret_marks import (
    SECRET_MARKS_ENV_VAR,
    format_secret_marks,
    merge_secret_marks,
    parse_secret_marks,
)


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


@dataclass
class EnvWrite:
    """What one operation's env write changed, so :func:`revert_env_write` reverses exactly that.

    ``values`` maps each value key written to its PRIOR store value and the value written
    (``""`` for an absent key). ``added_marks`` are the secret marks the write added to the
    ``TAI_ENV_SECRET_KEYS`` marks var (those it already held are not this write's).
    """

    values: dict[str, tuple[str, str]] = field(default_factory=dict)
    added_marks: list[str] = field(default_factory=list)


def _revert_plan(written: EnvWrite, stored: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The env changes that revert ``written`` against ``stored``, and the keys that cannot be reverted.

    A value key still holding its prior value needs no change; one still holding the value
    written goes back to its prior value; one holding any other value was rewritten by
    another write and cannot be reverted. The marks var keeps every mark but the ones
    ``written`` added, and is rewritten only while it still holds one of them.
    """
    changes: dict[str, str] = {}
    conflicts: list[str] = []
    for key, (prior, value) in written.values.items():
        current = stored.get(key, "")
        if current == prior:
            continue
        if current == value:
            changes[key] = prior
        else:
            conflicts.append(key)
    if written.added_marks:
        current_marks = parse_secret_marks(stored.get(SECRET_MARKS_ENV_VAR))
        if any(mark in current_marks for mark in written.added_marks):
            changes[SECRET_MARKS_ENV_VAR] = format_secret_marks(
                mark for mark in current_marks if mark not in written.added_marks
            )
    return changes, conflicts


async def revert_env_write(svc: ConfigService, cm: Any, written: EnvWrite | None) -> str | None:
    """Revert THIS operation's env contribution through the ConfigService env door.

    The revert is derived from the stored env read under the write exclusion
    (:meth:`~tai42_skeleton.config.service.ConfigService.apply_derived_env_change`), so every
    write another door landed since stands: a value key this operation wrote goes back to
    its PRIOR store value (``""`` deletes a key that was ABSENT before) while the store still
    holds the value written, and the ``TAI_ENV_SECRET_KEYS`` marks var loses only the marks
    this operation added. A value key another write replaced since raises
    :class:`EnvRevertConflictError` naming it, and the revert writes nothing. A store read
    outside the exclusion first decides whether a revert is due, so when nothing persisted
    (the pre-persist dangling refusal) no env change fires — no fleet broadcast on a no-op.
    Runs AFTER any manifest reversal, so no restored marker references a dropped key.

    Returns the revert write's failed span release (``None`` when it released, or nothing
    needed reverting).
    """
    if written is None or not (written.values or written.added_marks):
        return None
    try:
        current = cm.read_env()
    except FileNotFoundError:
        current = {}
    changes, conflicts = _revert_plan(written, current)
    if not changes and not conflicts:
        return None

    def derive(stored: dict[str, str]) -> dict[str, str]:
        changes, conflicts = _revert_plan(written, stored)
        if conflicts:
            raise EnvRevertConflictError(
                "cannot revert the env key(s) another write replaced since this operation wrote them: "
                + ", ".join(sorted(conflicts))
            )
        return changes

    return (await svc.apply_derived_env_change(derive)).span_release_error


async def apply_provides_change(
    svc: ConfigService,
    spec: PluginSpec,
    mutator: Callable[[dict[str, Any]], None],
    *,
    env: dict[str, str] | None,
    secret_keys: list[str] | None,
    env_to_write: dict[str, str],
    env_written: EnvWrite,
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

    Under the pipeline's config-write lock (against the SAME stored-env snapshot the write
    derives from) this records into ``env_written`` each value key's prior store value and
    the value written (``""`` for an absent key) and, when ``secret_keys`` are supplied, the
    marks the ``TAI_ENV_SECRET_KEYS`` merge adds. :func:`revert_env_write` reverts exactly
    those, so the unwind never deletes an operator's pre-existing store value or drops
    another write's secret marks.

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
        # Record what this write changes, from the STORED snapshot it derives from: each
        # value key's prior string (``""`` when absent) beside the value written, and the
        # marks the merge adds.
        env_written.values = {key: (stored.get(key, ""), value) for key, value in changes.items()}
        env_written.added_marks = []
        if marks:
            # Append to the marks read from the STORED env (never the settings
            # cache, stale until a reload), mirroring ``set_mcp_secret_env``.
            stored_marks = stored.get(SECRET_MARKS_ENV_VAR)
            env_written.values.pop(SECRET_MARKS_ENV_VAR, None)
            env_written.added_marks = [mark for mark in marks if mark not in parse_secret_marks(stored_marks)]
            changes[SECRET_MARKS_ENV_VAR] = merge_secret_marks(stored_marks, marks)
        return changes, mutator

    with translate_config_invariants(composed_manifest_invalid):
        try:
            return await svc.apply_env_and_change(prepare, manifest_pointer=manifest_pointer)
        except ValidationError as exc:
            raise composed_manifest_invalid(str(exc)) from exc
        except ValueError as exc:
            # A dangling-marker refusal (each missing required var + json-pointer) or
            # another env-boundary refusal — raised before anything persisted.
            raise InstallEnvError(str(exc)) from exc
