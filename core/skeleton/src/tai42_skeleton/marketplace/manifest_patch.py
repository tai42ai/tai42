"""Pure manifest-patch functions for a plugin's ``provides`` index.

Three side-effect-free functions over a plain manifest dict — no I/O, no app
handle — so each kind of provides item is unit-testable in isolation:
:func:`collisions` (a pre-flight that never mutates), :func:`apply_provides`
(the install patch), and :func:`remove_provides` (the uninstall unpatch). All
three are driven by :data:`~tai42_contract.plugins.KIND_MANIFEST_BINDINGS`: it
names the manifest field each item kind wires into and the patch shape.

The six patch shapes:

- ``config_row`` (``tools``, ``agents``) — one config entry per DISTINCT module,
  ``{"title": <module>, "module": <module>}``. The title IS the module path:
  deterministic, unique, and the marker that the entry is installer-owned.
- ``module_list`` (``extensions_modules``, ``channel_modules``,
  ``webhook_verifier_modules``, ``lifecycle_modules``, ``routers_modules``,
  ``middlewares_modules``) — the item's module appended to a plain module list.
  ``routers_modules`` is the one ordering-aware case: when the Studio SPA
  catch-all (:data:`~tai42_skeleton.app.route_defaults.STUDIO_SPA_ROUTER`) is
  present in the list, a new router is INSERTED before it rather than appended,
  because the catch-all matches every path and a router registered after it
  serves nothing. The catch-all's last position is a skeleton serving fact, not a
  contract rule; the contract binding stays a plain ``module_list``.
- ``package_list`` (``studio_plugins``) — the plugin's DISTRIBUTION name (not the
  item's module) appended to a package-name list.
- ``mcp_entry`` (``mcp``) — a ``{title: item.name, config: item.mcp}`` object
  appended to the manifest ``mcp`` list (an mcp-server item carries transport
  config in ``item.mcp`` and NO module, so the payload is built from
  ``item.name``/``item.mcp``, never ``item.module``). Deduped by title; a title
  already present (hand-written or previously installed) is a collision, never an
  overwrite; uninstall removes by title, convergently.
- ``descriptor_entry`` (``connectors``) — the item's ``provider`` descriptor
  (``item.provider.model_dump(mode="json", exclude_none=True)``) appended to the
  manifest ``connectors`` list (a connector item carries a ``ProviderDescriptor`` and
  NO module, so the payload is the descriptor itself). Deduped by ``id``; an ``id``
  already present (hand-written or previously installed) is a collision, never an
  overwrite; uninstall removes by ``id``, convergently.
- ``scalar_module`` (``backend_module``, ``sandbox_module``, ``storage_module``,
  ``monitoring_module``) — a single-module slot holding the plugin's TOP-LEVEL
  import package (``item.module`` up to its first dot), never the descriptor's
  impl submodule: the skeleton imports the slot and whitelists every module under
  that package root, so the package ``__init__`` registers the provider and its
  sibling tool/extension modules. A second plugin claiming an occupied slot is a
  collision, as is one spec providing two distinct modules for the same slot.
- ``env_selected`` (``config``) — no manifest field. A DECIDED no-op in all three
  functions: pip install/uninstall IS the whole registration, and activation goes
  through the skeleton's ``TAI_CONFIG_MODE`` naming convention (``file`` is built in;
  any other mode resolves to the installed ``tai42-config-<mode>`` provider, so no
  skeleton-side change is needed to select a newly installed one).

An unknown item kind — contract drift past this repo's bindings — raises
:class:`ManifestBindingError` naming it (a server-side 500), never a silently
skipped item.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, PluginItem, PluginSpec

from tai42_skeleton.app.route_defaults import STUDIO_SPA_ROUTER
from tai42_skeleton.marketplace.errors import ManifestBindingError, ManifestCollisionError


class _FieldTargets(NamedTuple):
    """The patch mode for one manifest field and the distinct payloads to apply.

    ``values`` is a per-mode payload union: string modes carry ``list[str]``
    (module paths or the distribution name); ``mcp_entry`` carries the built
    ``{title, config}`` dicts; ``descriptor_entry`` carries the dumped provider
    descriptor dicts. Each writer branches on ``mode`` before reading a payload, so
    the shapes never mix at one site."""

    mode: str
    values: list[Any]


# The item-value derivation the grouping shares between the two payload families:
# a ``data`` item's whole manifest object, and a non-data item's contributed string.
def _data_item_payload(item: PluginItem, mode: str) -> dict[str, Any]:
    """The manifest object a ``data`` item contributes: an ``mcp_entry`` item's
    ``{title, config}`` wrapper around its transport config, or a
    ``descriptor_entry`` item's dumped ``provider`` descriptor. The contract
    guarantees the declarative block the kind names is set."""
    if mode == "mcp_entry":
        assert item.mcp is not None
        return {"title": item.name, "config": item.mcp.model_dump(exclude_none=True)}
    assert item.provider is not None  # descriptor_entry
    return item.provider.model_dump(mode="json", exclude_none=True)


def _string_item_value(item: PluginItem, spec: PluginSpec, mode: str) -> str | None:
    """The manifest string a non-data item contributes: the plugin's DISTRIBUTION
    name for a ``package_list`` slot, the module's TOP-LEVEL import package for a
    ``scalar_module`` slot (never the descriptor's impl submodule, so the loader
    whitelists every module under the package root), else the item's own module."""
    if mode == "package_list":
        return spec.package
    if mode == "scalar_module":
        assert item.module is not None
        return item.module.partition(".")[0]
    return item.module


# The dedupe key of a ``data`` payload: an mcp entry is unique by title, a connector
# descriptor by id.
_DATA_DEDUPE_KEY = {"mcp_entry": "title", "descriptor_entry": "id"}


def _grouped_targets(spec: PluginSpec) -> dict[str, _FieldTargets]:
    """Group the spec's provides items by the manifest field they target.

    Every ``env_selected`` (``config``) item is skipped — it has no manifest
    field. Values are de-duplicated per field: multiple items sharing a module
    (or, for ``package_list``, the single distribution name) collapse to one
    entry; a ``data`` item is deduped by its identifying key. An item kind with no
    binding raises :class:`ManifestBindingError`.
    """
    grouped: dict[str, _FieldTargets] = {}
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is None:
            raise ManifestBindingError(f"no manifest binding for plugin item kind {item.kind.value!r}")
        if binding.mode == "env_selected":
            continue
        field = binding.field
        if field is None:  # pragma: no cover - guarded by the binding invariant
            raise ManifestBindingError(f"binding for kind {item.kind.value!r} names no field but is not env-selected")
        target = grouped.setdefault(field, _FieldTargets(mode=binding.mode, values=[]))
        if binding.payload == "data":
            payload = _data_item_payload(item, binding.mode)
            key = _DATA_DEDUPE_KEY[binding.mode]
            if not any(existing[key] == payload[key] for existing in target.values):
                target.values.append(payload)
        else:
            value = _string_item_value(item, spec, binding.mode)
            if value not in target.values:
                target.values.append(value)
    return grouped


def _existing_list(manifest_dict: dict[str, Any], field: str) -> list[Any]:
    """The current value of a list-shaped manifest field, or ``[]`` when unset."""
    value = manifest_dict.get(field)
    return value if isinstance(value, list) else []


def _ensure_list(manifest_dict: dict[str, Any], field: str) -> list[Any]:
    """The list-shaped manifest field, replacing an absent or non-list value with a
    fresh list stored in place so the caller appends into the live manifest."""
    entries = manifest_dict.get(field)
    if not isinstance(entries, list):
        entries = []
        manifest_dict[field] = entries
    return entries


# -- collision handlers, one per patch shape ---------------------------------


def _collide_config_row(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> list[str]:
    entries = _existing_list(manifest_dict, field)
    occupied = {entry.get("module") for entry in entries} | {entry.get("title") for entry in entries}
    return [f"{field} entry with module {module!r} already exists" for module in target.values if module in occupied]


def _collide_string_list(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> list[str]:
    entries = _existing_list(manifest_dict, field)
    return [f"{field} already contains {value!r}" for value in target.values if value in entries]


def _collide_mcp_entry(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> list[str]:
    titles = {entry.get("title") for entry in _existing_list(manifest_dict, field)}
    return [f"{field} entry titled {p['title']!r} already exists" for p in target.values if p["title"] in titles]


def _collide_descriptor_entry(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> list[str]:
    ids = {entry.get("id") for entry in _existing_list(manifest_dict, field)}
    return [f"{field} entry with id {p['id']!r} already exists" for p in target.values if p["id"] in ids]


def _collide_scalar_module(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> list[str]:
    current = manifest_dict.get(field)
    if current:
        return [f"{field} is already set to {current!r} (cannot install {module!r})" for module in target.values]
    if len(target.values) > 1:
        joined = ", ".join(repr(module) for module in target.values)
        return [f"{field} is a single-module slot but this plugin provides {joined}"]
    return []


_COLLIDE_HANDLERS: dict[str, Callable[[dict[str, Any], str, _FieldTargets], list[str]]] = {
    "config_row": _collide_config_row,
    "module_list": _collide_string_list,
    "package_list": _collide_string_list,
    "mcp_entry": _collide_mcp_entry,
    "descriptor_entry": _collide_descriptor_entry,
    "scalar_module": _collide_scalar_module,
}


# -- apply handlers, one per patch shape -------------------------------------


def _apply_config_row(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> None:
    entries = _ensure_list(manifest_dict, field)
    for module in target.values:
        entries.append({"title": module, "module": module})


def _apply_string_list(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> None:
    entries = _ensure_list(manifest_dict, field)
    for value in target.values:
        if value in entries:
            continue
        # ``routers_modules`` is ordering-aware: a router listed AFTER the Studio SPA
        # catch-all serves nothing (the catch-all matches every path), so insert each
        # new router BEFORE the sentinel when present, preserving the relative order of
        # multiple inserted routers. Every other list field (and routers_modules without
        # the sentinel, where the loader owns catch-all placement) plain-appends.
        if field == "routers_modules" and STUDIO_SPA_ROUTER in entries:
            entries.insert(entries.index(STUDIO_SPA_ROUTER), value)
        else:
            entries.append(value)


def _apply_mcp_entry(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> None:
    # The collisions() re-check in ``apply_provides`` rejects a title already present, so
    # a hand-written or previously-installed entry is never overwritten.
    entries = _ensure_list(manifest_dict, field)
    entries.extend(target.values)


def _apply_descriptor_entry(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> None:
    # The collisions() re-check in ``apply_provides`` rejects a provider id already
    # present, so a hand-written or previously-installed connector is never overwritten.
    entries = _ensure_list(manifest_dict, field)
    entries.extend(target.values)


def _apply_scalar_module(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> None:
    # The collisions() re-check rejects a spec with two distinct modules for one scalar
    # slot, so target.values holds at most one here.
    for module in target.values:
        manifest_dict[field] = module


_APPLY_HANDLERS: dict[str, Callable[[dict[str, Any], str, _FieldTargets], None]] = {
    "config_row": _apply_config_row,
    "module_list": _apply_string_list,
    "package_list": _apply_string_list,
    "mcp_entry": _apply_mcp_entry,
    "descriptor_entry": _apply_descriptor_entry,
    "scalar_module": _apply_scalar_module,
}


# -- removal handlers, one per patch shape -----------------------------------


def _remove_by_predicate(manifest_dict: dict[str, Any], field: str, keep: Callable[[Any], bool]) -> bool:
    """Drop every list entry ``keep`` rejects; report whether the field changed."""
    entries = _existing_list(manifest_dict, field)
    kept = [entry for entry in entries if keep(entry)]
    if len(kept) != len(entries):
        manifest_dict[field] = kept
        return True
    return False


def _remove_config_row(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> bool:
    modules = set(target.values)
    return _remove_by_predicate(manifest_dict, field, lambda entry: entry.get("module") not in modules)


def _remove_string_list(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> bool:
    values = set(target.values)
    return _remove_by_predicate(manifest_dict, field, lambda value: value not in values)


def _remove_mcp_entry(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> bool:
    titles = {payload["title"] for payload in target.values}
    return _remove_by_predicate(manifest_dict, field, lambda entry: entry.get("title") not in titles)


def _remove_descriptor_entry(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> bool:
    ids = {payload["id"] for payload in target.values}
    return _remove_by_predicate(manifest_dict, field, lambda entry: entry.get("id") not in ids)


def _remove_scalar_module(manifest_dict: dict[str, Any], field: str, target: _FieldTargets) -> bool:
    current = manifest_dict.get(field)
    if current is not None and current in target.values:
        manifest_dict[field] = None
        return True
    return False


_REMOVE_HANDLERS: dict[str, Callable[[dict[str, Any], str, _FieldTargets], bool]] = {
    "config_row": _remove_config_row,
    "module_list": _remove_string_list,
    "package_list": _remove_string_list,
    "mcp_entry": _remove_mcp_entry,
    "descriptor_entry": _remove_descriptor_entry,
    "scalar_module": _remove_scalar_module,
}


def collisions(manifest_dict: dict[str, Any], spec: PluginSpec) -> list[str]:
    """Human-readable descriptions of every provides item that cannot be applied
    cleanly against ``manifest_dict``; an empty list means the spec is safe to
    apply.

    A ``config_row`` item collides when an existing entry already carries that
    module or that title; a ``module_list``/``package_list`` item when the exact
    string is already present; a ``scalar_module`` item when the slot is already
    truthy, OR when the spec itself provides two distinct modules for one
    single-module slot (an intra-spec self-conflict a last-write-wins apply would
    otherwise silently drop). An ``mcp_entry`` item collides when an existing
    manifest ``mcp`` entry already carries that title (hand-written or previously
    installed) — never overwritten. A ``descriptor_entry`` item collides when an
    existing manifest ``connectors`` entry already carries that provider ``id``
    (hand-written or previously installed) — never overwritten. ``env_selected``
    (``config``) items never collide.
    """
    messages: list[str] = []
    for field, target in _grouped_targets(spec).items():
        messages.extend(_COLLIDE_HANDLERS[target.mode](manifest_dict, field, target))
    return messages


def apply_provides(manifest_dict: dict[str, Any], spec: PluginSpec) -> None:
    """Patch ``manifest_dict`` in place, adding one manifest reference per
    provides target.

    Re-checks :func:`collisions` first — the install pre-flight may have raced a
    foreign manifest edit — and raises :class:`ManifestCollisionError` listing
    every collision before mutating anything. ``env_selected`` (``config``) items
    add nothing.
    """
    found = collisions(manifest_dict, spec)
    if found:
        raise ManifestCollisionError("; ".join(found))
    for field, target in _grouped_targets(spec).items():
        _APPLY_HANDLERS[target.mode](manifest_dict, field, target)


def remove_provides(manifest_dict: dict[str, Any], spec: PluginSpec) -> bool:
    """Remove every manifest reference the spec's provides created; return
    whether anything changed.

    Convergent — an already-removed entry is skipped, so a re-run after a partial
    uninstall completes the removal. ``config_row`` entries are dropped by
    matching ``module`` regardless of title, because after the pip uninstall the
    import fails and a leftover entry bricks the next boot; any operator
    include/exclude/extensions customization on such an entry is removed with it
    (inherent to uninstalling the plugin). A ``scalar_module`` slot is cleared to
    ``None`` only while it still equals the spec's module — a foreign value means
    the operator replaced it, so it is left untouched. An ``mcp_entry`` item drops
    the manifest ``mcp`` entry matching its title; a title already gone is skipped
    (convergent), never an error. A ``descriptor_entry`` item drops the manifest
    ``connectors`` entry matching its provider ``id``; an id already gone is skipped
    (convergent), never an error. ``env_selected`` (``config``) items remove nothing.
    """
    changed = False
    for field, target in _grouped_targets(spec).items():
        if _REMOVE_HANDLERS[target.mode](manifest_dict, field, target):
            changed = True
    return changed
