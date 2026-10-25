"""Record views, write-response shaping, and the dry-run verdict shape.

The row / response dicts every preset door returns, built once so no two doors drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import PresetBody
from tai42_contract.template import TemplatedText

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.operations._broadcast import fleet_fanout

if TYPE_CHECKING:
    from tai42_skeleton.operations.presets.create import CreateCrossReferences


def _wire_body_field(value: Any) -> Any:
    """A body field as the record row carries it: a stored-body reference as its JSON object, else as is."""
    return value.model_dump(mode="json") if isinstance(value, TemplatedText) else value


def _store_record_view(
    name: str,
    active_version: int,
    body: PresetBody,
    *,
    uses: list[str],
    used_by: list[str],
) -> dict[str, Any]:
    """A store-backed record row: identity + active-body fields plus conflict and cross-reference metadata.

    The ``conflicted`` flag (name in the quarantine map) with its ``conflicted_reason`` (the human-readable
    cause, ``null`` when not conflicted), plus the ``uses`` / ``used_by`` cross-references (sorted OTHER
    active presets this body composes, and that compose it — see :func:`_reference_maps`). Takes the
    already-fetched active ``body`` and this row's two reference lists so the caller batches the reads (the
    list route) or reuses one read (the get route) rather than round-tripping per row.
    """
    mgr = instance.app.preset_manager
    return {
        "name": name,
        "base_tool": body.base_tool,
        "description": body.description,
        "active_version": active_version,
        "extensions": [list(combo) for combo in body.extensions],
        "output_schema": _wire_body_field(body.output_schema),
        "input_schema": _wire_body_field(body.input_schema),
        "input_map": _wire_body_field(body.input_map),
        "output_map": _wire_body_field(body.output_map),
        "conflicted": mgr.is_quarantined(name),
        "conflicted_reason": mgr.quarantine_reason(name),
        "uses": uses,
        "used_by": used_by,
    }


def _new_record_view(
    name: str,
    base_tool: str,
    description: str,
    extensions: list[list[ExtensionElement]],
    output_schema: TemplatedText | dict[str, Any] | None,
    input_schema: TemplatedText | dict[str, Any] | None,
    *,
    input_map: TemplatedText | str | None,
    output_map: TemplatedText | str | None,
    active_version: int,
    uses: list[str],
    used_by: list[str],
) -> dict[str, Any]:
    """The record shape a fresh create returns.

    The identity + active-body fields from the just-applied spec (a fresh preset is never conflicted), plus
    this row's ``uses`` / ``used_by`` cross-references (see :func:`_reference_maps`), computed by the caller
    from the post-write active-body population so the create response parses under the same record schema as
    the list / get rows.
    """
    return {
        "name": name,
        "base_tool": base_tool,
        "description": description,
        "active_version": active_version,
        "extensions": [list(combo) for combo in extensions],
        "output_schema": _wire_body_field(output_schema),
        "input_schema": _wire_body_field(input_schema),
        "input_map": _wire_body_field(input_map),
        "output_map": _wire_body_field(output_map),
        "conflicted": False,
        "conflicted_reason": None,
        "uses": uses,
        "used_by": used_by,
    }


async def _wire_snapshot(name: str) -> dict[str, Any] | None:
    """The serialized wire tool for ``name`` (``to_mcp_tool().model_dump()``), or ``None`` if unbound.

    The client-visible listing state the emit guard diffs across a reload.
    """
    tools = await instance.app.tools.get_tools()
    tool = tools.get(name)
    return None if tool is None else tool.to_mcp_tool().model_dump()


def _create_response(
    name: str,
    base_tool: str,
    description: str,
    extensions: list[list[ExtensionElement]],
    output_schema: TemplatedText | dict[str, Any] | None,
    input_schema: TemplatedText | dict[str, Any] | None,
    *,
    input_map: TemplatedText | str | None,
    output_map: TemplatedText | str | None,
    active_version: int,
    report: FleetResult,
    cross: CreateCrossReferences,
) -> dict[str, Any]:
    """The create response every create door returns, built once so no two doors drift.

    The fresh record view plus its ``uses`` / ``used_by``, with the per-worker rebind fan-out
    report embedded under ``fanout``. The cross-references are the population read just
    before the commit plus the new body, so a create that commits answers its record view;
    a write to another preset landing between that read and the response is not in it (the
    list and get rows read fresh). The ``fanout`` (the same shape the template writers
    return) is the read-your-writes barrier signal — proof the new binding propagated to
    every serving worker, not only the one that applied this call.
    """
    view = _new_record_view(
        name,
        base_tool,
        description,
        extensions,
        output_schema,
        input_schema,
        input_map=input_map,
        output_map=output_map,
        active_version=active_version,
        uses=cross.uses,
        used_by=cross.used_by,
    )
    view["fanout"] = fleet_fanout(report)
    return view


def _save_version_response(row: Any, report: FleetResult) -> dict[str, Any]:
    """The save-version response every save door returns, built once so no two doors drift.

    The new version row plus the per-worker rebind fan-out report embedded under ``fanout`` (mirrors the
    template writers) — the read-your-writes barrier proving the new version reached every serving worker.
    """
    return {**row.model_dump(), "fanout": fleet_fanout(report)}


def _verdict(error: str | None) -> dict[str, Any]:
    """A validation verdict — ``valid`` is the absence of an ``error``.

    The op returns 200 for BOTH outcomes: an invalid draft is a SUCCESSFUL validation, not a request failure.
    """
    return {"valid": error is None, "error": error}
