"""Tool-extension operations — read and author a tool's applied extension combos.

The ``extensions`` MAP on a ``tools``/``mcp`` config is the single source of truth
for which clip-on powers (chain/batch/monitor/…) a tool carries, decoupled from
``include``/``exclude`` (selection). These operations read and write that map
through the single :class:`~tai42_skeleton.config.service.ConfigService` pipeline: the
mutator upserts the providing config's ``extensions`` entry on the PRESERVED
document, then ConfigService validates, persists, reloads locally, and broadcasts
the reload — so the map, the bound branch tools, and the whole-fleet surface stay in
sync automatically (every worker re-reads the persisted manifest and rebinds).

The concrete app singleton (``from tai42_skeleton.app import instance``) is reached
for the live manifest's resolved selection maps, the MCP-bound-tool map, and the
extension registry — none of which ride the emitted ``live_manifest`` dict.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from tai42_contract.app.responses import ApplyResponse
from tai42_contract.manifest import ExtensionElement

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import translate_config_invariants
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.manifest import Manifest, union_tool_extensions
from tai42_skeleton.operations import BadRequestError, ConflictError, NotFoundError, operation
from tai42_skeleton.operations._broadcast import apply_response
from tai42_skeleton.operations.manifest.reads import normalized_extension_map, read_preserved_document
from tai42_skeleton.operations.response_models_group_b import ToolExtensionCombosModified, ToolExtensionsView


class ToolExtensionsUpdate(BaseModel):
    """Set a tool's applied extension combos — the full list of combos, authored losslessly.

    Each combo element is an extension name or a ``{"name", "config"}`` mapping
    binding author config. An empty list clears them.
    """

    combos: list[list[ExtensionElement]]


class ToolExtensionCombosModify(BaseModel):
    """Add and/or remove single extension combos on a tool, granular over the full ``set`` write.

    Each combo is authored losslessly, exactly as ``combos`` above.
    """

    add: list[list[ExtensionElement]] = Field(default_factory=list)
    remove: list[list[ExtensionElement]] = Field(default_factory=list)


def _live_manifest() -> Manifest:
    return instance.app.admin.live_manifest_typed


def _owning_configs(manifest: Manifest, name: str) -> list[tuple[str, str]]:
    """Every config that PROVIDES ``name``, as ``(kind, key)`` pairs.

    ``key`` is the module for a ``tools`` config, the title for an ``mcp`` config.
    A local tool is owned when its resolved selection recorded it under the
    config's module; an mcp tool when resolved selection OR the live
    mcp-bound-tool map recorded it under the config's title.

    A branch/composed tool (the tool an extension combo PRODUCES, e.g.
    ``weather_chain``) is never itself an extension TARGET — only its base tool is —
    so it reports no owner. The mcp-bound-tool map records branch names alongside
    bases, so without this guard an mcp branch tool would resolve a bogus owner and
    corrupt the manifest on write; excluding it makes a branch tool report no owner
    exactly as the base-only ``resolved_includes`` path already does for local
    tools.
    """
    if instance.app.tools.is_branch(name):
        return []
    owners: list[tuple[str, str]] = []
    owners.extend(
        ("tools", cfg.module) for cfg in manifest.tools if name in manifest.resolved_includes.get(cfg.module, set())
    )
    owners.extend(
        ("mcp", cfg.title)
        for cfg in manifest.mcp
        if name in manifest.resolved_includes.get(cfg.title, set())
        or name in instance.app.tools.mcp_bound_names(cfg.title)
    )
    return owners


def _other_mappers(manifest: Manifest, name: str, owner: tuple[str, str]) -> list[str]:
    """The configs OTHER than the providing one whose ``extensions`` map also carries ``name``.

    A write would leave their combos behind and the union GET would disagree with
    what was authored, so any such config is a 409.
    """
    kind, key = owner
    others: list[str] = []
    others.extend(
        f"tools:{cfg.module}"
        for cfg in manifest.tools
        if name in cfg.extensions and not (kind == "tools" and cfg.module == key)
    )
    others.extend(
        f"mcp:{cfg.title}"
        for cfg in manifest.mcp
        if name in cfg.extensions and not (kind == "mcp" and cfg.title == key)
    )
    return others


def _resolve_single_owner(manifest: Manifest, name: str) -> tuple[str, str]:
    """The ONE config that provides ``name`` as ``(kind, key)``, or a loud reject.

    No owner → 400, multiple owners → 400, a mapping split across configs → the
    409 consolidation error. Shared by every door that writes a tool's
    ``extensions`` entry so the resolution stays byte-identical.
    """
    owners = _owning_configs(manifest, name)
    if not owners:
        raise BadRequestError(f"tool {name!r} is not currently provided by any config")
    if len(owners) > 1:
        where = ", ".join(f"{kind}:{key}" for kind, key in owners)
        raise BadRequestError(
            f"tool {name!r} is provided by multiple configs ({where}); cannot determine which to edit"
        )
    owner = owners[0]
    conflicting = _other_mappers(manifest, name, owner)
    if conflicting:
        raise ConflictError(
            f"tool {name!r} extensions are also mapped by other config(s) ({', '.join(conflicting)}); "
            "consolidate the mapping into the providing config first"
        )
    return owner


def _stored_tool_combos(document: dict[str, Any], name: str) -> list[list[ExtensionElement]]:
    """Every combo the stored manifest ``document`` attaches to ``name``, ``!ENV`` markers intact.

    The preserved ``tools``, ``mcp`` and ``api_tools`` maps are normalized to the stored shape
    and unioned in the manifest's own order, so the answer lists what is written, never a
    resolved value.
    """
    entry_maps = [
        normalized_extension_map(entry["extensions"])
        for section in ("tools", "mcp")
        for entry in document.get(section, [])
        if entry.get("extensions")
    ]
    api_tools_map = normalized_extension_map(document.get("api_tools", {}).get("extensions") or {})
    return union_tool_extensions(entry_maps, api_tools_map).get(name, [])


def _stored_owner_combos(document: dict[str, Any], owner: tuple[str, str], name: str) -> list[list[ExtensionElement]]:
    """The write target's OWN stored combos for ``name``, ``!ENV`` markers intact.

    After ``_resolve_single_owner``'s gates this is the union view, and it is what the
    granular merge edits. The entry is found as :func:`_apply_combos` finds it; an owner
    that left the stored ``document`` (a concurrent edit) raises ``BadRequestError`` rather
    than fabricating an empty list.
    """
    kind, key = owner
    section, key_field = ("tools", "module") if kind == "tools" else ("mcp", "title")
    entry = next((entry for entry in document.get(section, []) if entry.get(key_field) == key), None)
    if entry is None:
        raise BadRequestError(f"providing config for tool {name!r} not found in the manifest")
    return normalized_extension_map(entry.get("extensions") or {}).get(name, [])


def _normalized_combos(name: str, combos: list[list[ExtensionElement]]) -> list[list[ExtensionElement]]:
    """Combos in the exact stored form the contract normalization produces.

    So add/remove membership compares against the PERSISTED shape rather than the router
    edge's element reader.
    """
    if not combos:
        return []
    return normalized_extension_map({name: combos})[name]


def _validate_combos_against_registry(combos: list[list[ExtensionElement]]) -> None:
    """Reject an unknown extension name and a double-non-stackable-kind combo against the LIVE registry.

    Runs before any persist — one bad combo fails the whole write. Delegates to
    the ``app.extensions.validate_combo`` accessor (the shared combo-validation
    seam) so the manifest can never carry an illegal combo.
    """
    for combo in combos:
        try:
            instance.app.extensions.validate_combo(combo)
        except TaiValidationError as exc:
            raise BadRequestError(str(exc)) from exc


def _apply_combos(
    manifest_dict: dict[str, Any], owner: tuple[str, str], name: str, combos: list[list[ExtensionElement]]
) -> bool:
    """Upsert the providing config's ``extensions[name]`` entry in the persisted manifest dict.

    ``include``/``exclude`` are untouched. An empty ``combos`` drops the tool's
    key (and the whole ``extensions`` map if it empties), never writing a
    present-but-empty value. Returns whether the providing config entry was found.
    """
    kind, key = owner
    section, key_field = ("tools", "module") if kind == "tools" else ("mcp", "title")
    for entry in manifest_dict.get(section, []):
        if entry.get(key_field) == key:
            extensions = dict(entry.get("extensions", {}))
            if combos:
                extensions[name] = combos
            else:
                extensions.pop(name, None)
            if extensions:
                entry["extensions"] = extensions
            else:
                entry.pop("extensions", None)
            return True
    return False


@operation(
    summary="Get a tool's applied extension combos",
    tags=["extensions"],
    errors=[NotFoundError],
    response_model=ToolExtensionsView,
)
async def get_tool_extensions(name: str) -> dict:
    """Return the tool's applied extension combos and the available extensions; 404 for an unknown tool.

    The combos are served as stored, ``!ENV`` markers intact; no resolved value leaves on the wire.
    """
    tools = await instance.app.tools.get_tools()
    if name not in tools:
        raise NotFoundError(f"tool {name!r} is not a registered tool")
    combos = _stored_tool_combos(read_preserved_document(), name)
    return {"combos": combos, "available": instance.app.extensions.available_extensions()}


@operation(
    summary="Set a tool's applied extension combos",
    tags=["extensions"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError],
    request_model=ToolExtensionsUpdate,
    response_model=ApplyResponse,
)
async def set_tool_extensions(name: str, combos: list[list[ExtensionElement]]) -> object:
    """Author all of a tool's extension combos through the pipeline (persist, reload, broadcast).

    Owner resolution, the consolidation-conflict check, and the registry combo
    validation all run BEFORE the pipeline (nothing persisted on a reject). The
    mutator then upserts the providing config's ``extensions`` entry, and
    ConfigService validates the whole document, persists, reloads locally, and
    broadcasts the reload so every worker rebinds; the per-worker fleet report rides
    the response as its ``fanout`` summary. The persist + local reload have already
    landed, so re-running the apply (or a ``reload_config``) is the recovery for a
    sibling that did not converge.
    """
    manifest = _live_manifest()
    owner = _resolve_single_owner(manifest, name)

    _validate_combos_against_registry(combos)

    def mutator(document: dict[str, Any]) -> None:
        # Upsert the providing config's ``extensions[name]`` on the PRESERVED
        # document. A providing config that vanished from the persisted manifest (a
        # concurrent edit) refuses loudly rather than writing a config that is gone.
        if not _apply_combos(document, owner, name, combos):
            raise BadRequestError(f"providing config for tool {name!r} not found in the manifest")

    # ConfigService validates the whole document with the new map BEFORE persisting,
    # so a malformed entry rejects loudly (400) instead of corrupting the store.
    with translate_config_invariants(BadRequestError):
        try:
            result = await ConfigService.from_app().apply_change(mutator)
        except ValueError as exc:
            raise BadRequestError(f"invalid extensions for tool {name!r}: {exc}") from exc
    return apply_response(result)


def _merge_combo_edits(current: list[Any], add_combos: list[Any], remove_combos: list[Any], name: str) -> list[Any]:
    """The merged combo list — ``current`` plus ``add_combos`` minus ``remove_combos``.

    With the two membership guards: an add already attached is a loud 400, a
    remove not attached a loud 404 (each naming the combo). Returns kept combos
    (removals dropped) then the additions, in order.
    """
    for combo in add_combos:
        if combo in current:
            raise BadRequestError(f"combo {json.dumps(combo)} is already attached to tool {name!r}")
    for combo in remove_combos:
        if combo not in current:
            raise NotFoundError(f"combo {json.dumps(combo)} is not attached to tool {name!r}")
    return [combo for combo in current if combo not in remove_combos] + add_combos


@operation(
    summary="Add or remove single extension combos on a tool",
    tags=["extensions"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=ToolExtensionCombosModify,
    response_model=ToolExtensionCombosModified,
)
async def modify_tool_extension_combos(
    name: str,
    add: list[list[ExtensionElement]] | None = None,
    remove: list[list[ExtensionElement]] | None = None,
) -> dict:
    """Granularly edit a tool's extension combos: append ``add`` and drop ``remove``.

    Applied over the merge of its current combos, then persisted through the same
    pipeline ``set_tool_extensions`` rides. Pinned order (each check loud, nothing
    persisted on a reject): the tool must be
    REGISTERED (404, as ``get_tool_extensions``); its owning config resolves
    (400/400/409, as ``set_tool_extensions``); membership over the OWNING config's
    combos in their stored form — adding a present combo → 400, removing an absent
    one → 404, each naming the combo; the MERGED list validates against the live
    registry (400) before any write. Returns the tool's full new combos and the catalog in the
    GET door's shape, served as stored, ``!ENV`` markers intact (no resolved value leaves on the
    wire), plus the write's ``span_release_error``: the store's error when its exclusive write
    span failed to release after the persist, ``None`` when it released.
    """
    add = add or []
    remove = remove or []
    if not add and not remove:
        raise BadRequestError("at least one of 'add' or 'remove' must carry a combo")

    tools = await instance.app.tools.get_tools()
    if name not in tools:
        raise NotFoundError(f"tool {name!r} is not a registered tool")

    manifest = _live_manifest()
    owner = _resolve_single_owner(manifest, name)

    add_combos = _normalized_combos(name, add)
    remove_combos = _normalized_combos(name, remove)

    def mutator(document: dict[str, Any]) -> None:
        # The membership guards and the merge read ``document``, the stored manifest the
        # write exclusion holds, so a concurrent edit is merged over, never overwritten.
        merged = _merge_combo_edits(_stored_owner_combos(document, owner, name), add_combos, remove_combos, name)
        _validate_combos_against_registry(merged)
        _apply_combos(document, owner, name, merged)

    with translate_config_invariants(BadRequestError):
        try:
            result = await ConfigService.from_app().apply_change(mutator)
        except ValueError as exc:
            raise BadRequestError(f"invalid extensions for tool {name!r}: {exc}") from exc

    # The answer is the persisted document: the seal may retag a resolved value the mutator wrote.
    if result.document is None:
        raise AssertionError
    return {
        "combos": _stored_tool_combos(result.document, name),
        "available": instance.app.extensions.available_extensions(),
        "span_release_error": result.span_release_error,
    }
