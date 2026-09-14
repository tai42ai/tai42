"""The name-lifecycle doors: rename a preset (its name IS its live tool) and delete one."""

from __future__ import annotations

import logging
import sys
from typing import Any

from tai42_contract.presets.errors import (
    PresetExistsError,
    PresetNameConflictError,
    PresetNotFoundError,
)
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.operations import BadRequestError, ConflictError, NotFoundError, operation
from tai42_skeleton.operations._broadcast import fleet_fanout
from tai42_skeleton.operations.presets import fanout
from tai42_skeleton.operations.presets.authoring import _enforce_registration_tier
from tai42_skeleton.operations.presets.models import PresetRename
from tai42_skeleton.operations.presets.references import _delete_referees, _rename_referees
from tai42_skeleton.operations.response_models_group_a import PresetDeleteResult, PresetRenameResult
from tai42_skeleton.presets.manager import is_valid_preset_name

logger = logging.getLogger(__name__)

# This submodule's own package generation, captured at import time (a reload builds a
# fresh package + submodules together, so each generation's submodule reads its OWN
# package). ``_agent_tool_names`` is read through it at call time, so a test's
# ``monkeypatch.setattr`` on the package attribute is honored.
_pkg = sys.modules["tai42_skeleton.operations.presets"]


async def _check_rename_target(mgr: Any, new_name: str) -> None:
    """NEW-name pre-checks in create's exact order and codes: the quarantine 409 → the
    live-tool collision 409 → the agent tool-name collision 400 → the duplicate-preset
    409. Raises the mapped 400/409. Tool-name safety of ``new_name`` is validated at the
    top of ``rename_preset``, before any existence check, so it is not repeated here."""
    if mgr.is_quarantined(new_name):
        raise ConflictError(f"a quarantined preset {new_name!r} exists — delete the quarantined record first")
    if await mgr.name_conflicts(new_name):
        raise ConflictError(f"preset name {new_name!r} collides with an existing tool")
    # Read the agent-name space through the package object so a monkeypatch bites.
    if new_name in _pkg._agent_tool_names():
        raise BadRequestError(f"preset name {new_name!r} collides with an agent tool name")
    if mgr.is_registered(new_name):
        raise ConflictError(f"preset {new_name!r} already exists")


async def _rebind_new_then_drop_old(mgr: Any, store: Any, name: str, new_name: str) -> Any:
    """The local apply of a rename, NEW FIRST: move the store key, bind ``new_name`` from
    the moved row's active body (compensating by re-pointing the store back on a
    re-register failure so store + live never diverge), tear the OLD binding down, then
    re-key the tool_meta overlay. Returns the moved store record; raises the mapped 409s."""
    # Move the store key. The pre-checks make the typed conflicts race-window catches,
    # mapped exactly as create maps its post-write errors.
    try:
        record = await store.rename_preset(name, new_name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    except PresetExistsError as exc:
        raise ConflictError(f"preset {new_name!r} already exists") from exc
    except PresetNameConflictError as exc:
        raise ConflictError(f"preset name {new_name!r} collides with an existing tool") from exc

    # Local apply, NEW FIRST: bind ``new_name`` from the moved store row's active body.
    # Reload-before-remove keeps every call resolvable — during the window BOTH names
    # are bound and the old binding still works (its baked spec is in-memory, its base
    # tool untouched). On a re-register failure, compensate by re-pointing the store
    # back so store + live never diverge, then surface loudly; the OLD binding was
    # never touched, so the preset stays fully live under its old name.
    try:
        await mgr.reload(new_name)
    except Exception as reload_exc:
        try:
            await store.rename_preset(new_name, name)
        except Exception as compensate_exc:
            logger.exception("failed to re-point store row for preset %r after a rename re-register failure", name)
            raise compensate_exc from reload_exc
        if isinstance(reload_exc, PresetExistsError):
            raise ConflictError(f"preset {new_name!r} already exists") from reload_exc
        if isinstance(reload_exc, PresetNameConflictError):
            raise ConflictError(f"preset name {new_name!r} collides with an existing tool") from reload_exc
        raise reload_exc

    # Then tear the OLD binding down. A failure here leaves BOTH names bound (old is
    # stale-but-functional: its baked spec is in-memory and its base tool is untouched);
    # it re-raises loudly and ``reload_config`` is the documented recovery (rehydration
    # rebuilds from the store, which now knows only ``new_name``). The store move is
    # never unwound here — ``new_name`` is live and correct.
    await mgr.remove(name)

    # Re-key the tool_meta overlay AFTER the versioned rename's rollback window has
    # closed (after ``mgr.remove`` — the point past which the rename is never
    # unwound). An overlay re-keyed earlier would be stranded under the new name if a
    # reload failure rolled the versioned store back to the old name. The re-key
    # atomically drops any pre-existing ``new_name`` overlay row before moving the old
    # one (clean slate); a failure here raises loudly, leaving only a dangling old-name
    # row that the next claim reclaims. Guarded on the overlay store: with tool_meta
    # OFF the re-key is a no-op rather than a 500 opening an absent Postgres.
    if component_store_configured(SKELETON_COMPONENT):
        await instance.app.tool_meta.store.rename_tool(name, new_name)
    return record


@operation(
    summary="Rename a preset",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=PresetRename,
    response_model=PresetRenameResult,
)
async def rename_preset(name: str, new_name: str) -> dict[str, Any]:
    """Rename a preset, ATOMIC (a preset's name IS its live tool name). Runs create's
    ordered name pre-checks on the NEW name, BLOCKS with a 409 listing every referee
    if any live reference composes the current name, binds the new tool BEFORE tearing
    the old one down, fires one ``list_changed``, and fans the rebind out NEW-first
    then the old removal. The response embeds the primary rebind (new-name) fan-out
    report under ``fanout``; the old-name removal stays log-only.

    A preset's NAME is its identity everywhere — it IS the live tool binding, and every
    reference (preset bodies, platform wiring, plugin holders) keys on it deliberately;
    rename integrity is enforced at THIS gate, and there are no surrogate ids."""
    # The new name is a live tool name + a ``{name}`` route segment, so it must be
    # tool-name-safe — the same rule create enforces. Validated FIRST, before any
    # quarantine/existence/tier check, so an invalid new name is a 400 no matter the
    # current preset's state (a missing or quarantined or fenced preset still yields
    # 400, never 404/409/403).
    if not is_valid_preset_name(new_name):
        raise BadRequestError(f"invalid preset name {new_name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$")
    # A no-op rename is a caller error, surfaced loudly — never a silent 200.
    if new_name == name:
        raise BadRequestError("new name must differ from the current name")

    mgr = instance.app.preset_manager
    # A conflicted record was never registered and its name may be owned by a foreign
    # tool — rename must not touch it, nor launder a quarantined record into a clean
    # name (the delete-only stance save/rollback take).
    if mgr.is_quarantined(name):
        raise ConflictError(f"preset {name!r} is conflicted and is delete-only")

    # A store-less deploy holds no preset, so an unquarantined name is a genuine 404
    # without a Postgres open (delete's reasoning).
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"preset {name!r} not found")

    store = instance.app.presets.store
    try:
        await store.get_preset(name)
        active_body = await store.get_active_body(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    # Registration-tier fence: the tier is the CURRENT preset's base tool, so
    # renaming a fenced base tool's preset is admin-fenced too. Runs BEFORE the store move.
    await _enforce_registration_tier(active_body.base_tool)

    await _check_rename_target(mgr, new_name)

    # Referential integrity: BLOCK (never silently cascade-rewrite) a rename that would
    # strand any live reference — the FULL union of preset-body referees + every
    # registered referee (platform wiring: schedules/hooks/routes/extensions/parks, and
    # plugin holders), listing every holder so the operator updates them first. A referee
    # raising fails the rename loudly (no silent bypass).
    referees = await _rename_referees(name)
    if referees:
        raise ConflictError(
            f"preset {name!r} cannot be renamed — it is still referenced by: {referees}; update those references first"
        )

    # The pre-rename snapshot judges the RELOAD, and is the floor for the remove:
    # both halves are one rename, so a worker owed the rename from the start stays
    # owed it at the life it had then. The remove's set is WIDER — see the union
    # assembled after the reload below.
    census = await fanout._census_at_start(fanout._RELOAD_OP)

    record = await _rebind_new_then_drop_old(mgr, store, name, new_name)

    # A rename changes the tool listing by definition (old gone, new present), so the
    # emit is unconditional — no wire-diff guard.
    await instance.app.emit_list_changed("tool")
    # Fan out NEW FIRST — reload ``new_name`` on every worker BEFORE removing ``old``
    # (both briefly alive beats neither): the two are sequentially awaited confirmed
    # broadcasts, so every worker applies the reload before any is asked to remove.
    reload_report = await fanout._fanout_reload(new_name, census)
    addressed = fanout._addressed_siblings(reload_report)
    # Every worker the reload actually reached is now bound to ``new_name`` and must
    # also be told to drop the old one — including a worker that joined after the
    # snapshot (it rehydrated the OLD name before the commit and announced itself
    # after, so it holds a binding the store no longer knows).
    await fanout._fanout_remove(
        name, fanout._union_census(census, addressed, await fanout._census_at_start(fanout._REMOVE_OP))
    )
    # Embed the primary rebind fan-out report — the propagation of the NEW binding, the
    # read-your-writes barrier a deployer checks. The old-name removal is teardown and
    # stays log-only (a single ``fanout`` field mirrors the template writers exactly; a
    # rename does not invent a two-report shape).
    return {
        "name": new_name,
        "renamed_from": name,
        "active_version": record.active_version,
        "fanout": fleet_fanout(reload_report),
    }


@operation(
    summary="Delete a preset",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[ConflictError, NotFoundError],
    response_model=PresetDeleteResult,
)
async def delete_preset(name: str) -> dict[str, Any]:
    """Delete a preset. A non-conflicted record is soft-deleted and its base + branch
    tools torn down (one ``list_changed``); a conflicted record is removed store-side
    ONLY (HARD delete + drop the quarantine entry), touching no registration and
    firing no emit. Both branches fan the removal out on the bus and embed the
    per-worker fleet report under ``fanout``."""
    mgr = instance.app.preset_manager

    # Consult the delete referees FIRST (before any teardown): a referee cascades its own
    # cleanup and returns empty, or vetoes with the references it will not let the delete
    # strand. Any non-empty answer blocks the delete — nothing is torn down.
    blockers = await _delete_referees(name)
    if blockers:
        raise ConflictError(f"preset {name!r} cannot be deleted — held by: {'; '.join(blockers)}")

    if mgr.is_quarantined(name):
        # A conflicted record was never registered — remove ONLY the stored
        # document (HARD delete, so no ghost/version history lingers), drop the
        # quarantine entry immediately, and touch NO registration (the name may be
        # owned by a foreign tool) and fire NO emit. The removal still fans out so a
        # sibling's in-memory quarantine entry is cleared too. A quarantine entry only
        # ever arises from a store-backed preset (rehydrate or reconcile of a created
        # preset — both require a configured store), so the hard-delete here always has
        # a store to talk to; the store-config guard below is only for the
        # non-quarantined path.
        census = await fanout._census_at_start(fanout._REMOVE_OP)
        try:
            await instance.app.versioning.store.delete("preset", name)
        except Exception:
            logger.exception("failed to hard-delete conflicted preset record %r", name)
            raise
        # Cascade the overlay row: the tool is gone, so its organizational
        # metadata goes with it. A no-op when the preset never got a row, and skipped
        # entirely when the overlay store is OFF (no absent-Postgres open).
        if component_store_configured(SKELETON_COMPONENT):
            await instance.app.tool_meta.store.delete_meta(name)
        mgr.drop_quarantine(name)
        report = await fanout._fanout_remove(name, census)
        # Embed the per-worker removal fan-out report (mirrors the template writers): a
        # deployer reads it as proof the teardown reached every serving worker.
        return {"name": name, "deleted": True, "fanout": fleet_fanout(report)}

    # A store-less deploy (no versioned store configured) can hold no preset, so a
    # name that is not quarantined is a genuine 404 without a Postgres read.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"preset {name!r} not found")
    census = await fanout._census_at_start(fanout._REMOVE_OP)
    try:
        await instance.app.presets.store.soft_delete(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    await mgr.remove(name)
    # Cascade the overlay row once the preset is soft-deleted and torn down.
    # Keyed by tool name, so the soft-delete ghost in ``versioned_documents`` is
    # irrelevant; a no-op when the preset never got a row, and skipped entirely when
    # the overlay store is OFF (no absent-Postgres open).
    if component_store_configured(SKELETON_COMPONENT):
        await instance.app.tool_meta.store.delete_meta(name)
    await instance.app.emit_list_changed("tool")
    report = await fanout._fanout_remove(name, census)
    # Embed the per-worker removal fan-out report (mirrors the template writers): a
    # deployer reads it as proof the teardown reached every serving worker.
    return {"name": name, "deleted": True, "fanout": fleet_fanout(report)}
