"""The delete door: remove a preset (its name IS its live tool) with the branch tools it binds."""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.errors import OperationError
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.operations import ConflictError, NotFoundError, operation
from tai42_skeleton.operations._broadcast import fleet_fanout
from tai42_skeleton.operations.errors import OperationFailedError
from tai42_skeleton.operations.holders import bound_branches, delete_holders, holders_refusal
from tai42_skeleton.operations.presets import fanout
from tai42_skeleton.operations.presets.read import require_preset
from tai42_skeleton.operations.response_models.presets import PresetDeleteResult

logger = logging.getLogger(__name__)


@operation(
    summary="Delete a preset",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[ConflictError, NotFoundError, OperationFailedError],
    response_model=PresetDeleteResult,
)
async def delete_preset(name: str) -> dict[str, Any]:
    """Delete a preset.

    Order: existence → refuse with a 409 listing every holder of the preset or one of its
    extension branch tools → every delete cascade for each of those names → removal → the
    overlay rows of those names cleared. A cascade that raises fails the delete before anything is
    removed: its typed operation error as it is, any other raise as a ``500`` naming the cascade's
    reason; the cascades already run are not undone. A non-conflicted record is soft-deleted and its base + branch
    tools torn down (one ``list_changed``); a conflicted record is removed store-side ONLY (HARD delete +
    drop the quarantine entry), touching no registration and firing no emit. Both branches fan the
    removal out on the bus and embed the per-worker fleet report under ``fanout``.
    """
    mgr = instance.app.preset_manager
    quarantined = mgr.is_quarantined(name)
    await require_preset(name)

    # The one read of the branch tools the delete removes; the gate, the cascades and the
    # overlay cleanup all walk it.
    removed = [name, *bound_branches(name)]
    holders = await delete_holders(name, removed[1:])
    if holders:
        raise await holders_refusal(f"preset {name!r} cannot be deleted", holders, "remove those references first")
    # Cleanup runs only once nothing holds the name, and before the record is removed; a
    # cascade raising fails the delete with the preset intact, answering the cascade's own
    # typed error as it is and any other raise as a 500 naming the preset, the tool and the reason.
    for cascade in instance.app.tools.delete_cascades():
        for tool_name in removed:
            try:
                await cascade(tool_name)
            except OperationError:
                raise
            except Exception as exc:
                raise OperationFailedError(
                    f"preset {name!r} cannot be deleted — a delete cascade failed for {tool_name!r}: {exc}"
                ) from exc

    if quarantined:
        # A conflicted record was never registered — remove ONLY the stored
        # document (HARD delete, so no ghost/version history lingers), drop the
        # quarantine entry immediately, and touch NO registration (the name may be
        # owned by a foreign tool) and fire NO emit. The removal still fans out so a
        # sibling's in-memory quarantine entry is cleared too. A quarantine entry only
        # ever arises from a store-backed preset, so the hard-delete here always has
        # a store to talk to.
        census = await fanout._census_at_start(fanout._REMOVE_OP)
        try:
            await instance.app.versioning.store.delete("preset", name)
        except Exception:
            logger.exception("failed to hard-delete conflicted preset record %r", name)
            raise
        await _clear_overlay_rows(removed)
        mgr.drop_quarantine(name)
        report = await fanout._fanout_remove(name, census)
        # Embed the per-worker removal fan-out report (mirrors the template writers): a
        # deployer reads it as proof the teardown reached every serving worker.
        return {"name": name, "deleted": True, "fanout": fleet_fanout(report)}

    census = await fanout._census_at_start(fanout._REMOVE_OP)
    try:
        await instance.app.presets.store.soft_delete(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    await mgr.remove(name)
    await _clear_overlay_rows(removed)
    await instance.app.emit_list_changed("tool")
    report = await fanout._fanout_remove(name, census)
    # Embed the per-worker removal fan-out report (mirrors the template writers): a
    # deployer reads it as proof the teardown reached every serving worker.
    return {"name": name, "deleted": True, "fanout": fleet_fanout(report)}


async def _clear_overlay_rows(names: list[str]) -> None:
    """Clear the tool_meta overlay row of each removed tool: the tool is gone, so its organization goes with it.

    Keyed by tool name, so a soft-delete ghost is irrelevant; a no-op for a name with no row, and
    skipped when the overlay store is OFF (no absent-Postgres open).
    """
    if component_store_configured(SKELETON_COMPONENT):
        for tool_name in names:
            await instance.app.tool_meta.store.delete_meta(tool_name)
