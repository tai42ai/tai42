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
- ``scalar_module`` (``backend_module``, ``storage_module``,
  ``monitoring_module``) — a single-module slot; a second plugin claiming an
  occupied slot is a collision, as is one spec providing two distinct modules for
  the same slot.
- ``env_selected`` (``config``) — no manifest field. A DECIDED no-op in all three
  functions: pip install/uninstall IS the whole registration, and activation goes
  through the skeleton's fixed ``TAI_CONFIG_MODE`` → module map (a new config
  provider needs a skeleton-side enum/map entry before any env var can select it).

An unknown item kind — contract drift past this repo's bindings — raises
:class:`ManifestBindingError` naming it (a server-side 500), never a silently
skipped item.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, PluginSpec

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


def _grouped_targets(spec: PluginSpec) -> dict[str, _FieldTargets]:
    """Group the spec's provides items by the manifest field they target.

    Every ``env_selected`` (``config``) item is skipped — it has no manifest
    field. Values are de-duplicated per field: multiple items sharing a module
    (or, for ``package_list``, the single distribution name) collapse to one
    entry. An item kind with no binding raises :class:`ManifestBindingError`.
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
        target = grouped.get(field)
        if target is None:
            target = _FieldTargets(mode=binding.mode, values=[])
            grouped[field] = target
        if binding.payload == "data":
            # A data item carries its kind's declarative block and no module. The
            # ``mcp_entry`` shape wraps ``item.mcp`` as a ``{title, config}`` object
            # (deduped by title); ``descriptor_entry`` appends the item's
            # ``provider`` descriptor itself (deduped by id). The contract guarantees
            # the block the kind names is set.
            if binding.mode == "mcp_entry":
                assert item.mcp is not None
                mcp_payload = {"title": item.name, "config": item.mcp.model_dump(exclude_none=True)}
                if not any(existing["title"] == mcp_payload["title"] for existing in target.values):
                    target.values.append(mcp_payload)
            else:  # descriptor_entry
                assert item.provider is not None
                provider_payload = item.provider.model_dump(mode="json", exclude_none=True)
                if not any(existing["id"] == provider_payload["id"] for existing in target.values):
                    target.values.append(provider_payload)
            continue
        value = spec.package if binding.mode == "package_list" else item.module
        if value not in target.values:
            target.values.append(value)
    return grouped


def _existing_list(manifest_dict: dict[str, Any], field: str) -> list[Any]:
    """The current value of a list-shaped manifest field, or ``[]`` when unset."""
    value = manifest_dict.get(field)
    return value if isinstance(value, list) else []


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
        if target.mode == "config_row":
            entries = _existing_list(manifest_dict, field)
            occupied = {entry.get("module") for entry in entries} | {entry.get("title") for entry in entries}
            for module in target.values:
                if module in occupied:
                    messages.append(f"{field} entry with module {module!r} already exists")
        elif target.mode in ("module_list", "package_list"):
            entries = _existing_list(manifest_dict, field)
            for value in target.values:
                if value in entries:
                    messages.append(f"{field} already contains {value!r}")
        elif target.mode == "mcp_entry":
            entries = _existing_list(manifest_dict, field)
            titles = {entry.get("title") for entry in entries}
            for payload in target.values:
                if payload["title"] in titles:
                    messages.append(f"{field} entry titled {payload['title']!r} already exists")
        elif target.mode == "descriptor_entry":
            entries = _existing_list(manifest_dict, field)
            ids = {entry.get("id") for entry in entries}
            for payload in target.values:
                if payload["id"] in ids:
                    messages.append(f"{field} entry with id {payload['id']!r} already exists")
        elif target.mode == "scalar_module":
            current = manifest_dict.get(field)
            if current:
                for module in target.values:
                    messages.append(f"{field} is already set to {current!r} (cannot install {module!r})")
            elif len(target.values) > 1:
                joined = ", ".join(repr(module) for module in target.values)
                messages.append(f"{field} is a single-module slot but this plugin provides {joined}")
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
        if target.mode == "config_row":
            entries = manifest_dict.get(field)
            if not isinstance(entries, list):
                entries = []
                manifest_dict[field] = entries
            for module in target.values:
                entries.append({"title": module, "module": module})
        elif target.mode in ("module_list", "package_list"):
            entries = manifest_dict.get(field)
            if not isinstance(entries, list):
                entries = []
                manifest_dict[field] = entries
            for value in target.values:
                if value in entries:
                    continue
                # ``routers_modules`` is ordering-aware: a router listed AFTER the
                # Studio SPA catch-all serves nothing (the catch-all matches every
                # path), so insert each new router BEFORE the sentinel when present,
                # preserving the relative order of multiple inserted routers. Every
                # other module_list field (and routers_modules without the sentinel,
                # the case where the loader owns catch-all placement) plain-appends.
                if field == "routers_modules" and STUDIO_SPA_ROUTER in entries:
                    entries.insert(entries.index(STUDIO_SPA_ROUTER), value)
                else:
                    entries.append(value)
        elif target.mode == "mcp_entry":
            # The collisions() re-check above rejects a title already present, so a
            # hand-written or previously-installed entry is never overwritten.
            entries = manifest_dict.get(field)
            if not isinstance(entries, list):
                entries = []
                manifest_dict[field] = entries
            for payload in target.values:
                entries.append(payload)
        elif target.mode == "descriptor_entry":
            # The collisions() re-check above rejects a provider id already present, so a
            # hand-written or previously-installed connector is never overwritten.
            entries = manifest_dict.get(field)
            if not isinstance(entries, list):
                entries = []
                manifest_dict[field] = entries
            for payload in target.values:
                entries.append(payload)
        elif target.mode == "scalar_module":
            # The collisions() re-check above rejects a spec with two distinct
            # modules for one scalar slot, so target.values holds at most one here.
            for module in target.values:
                manifest_dict[field] = module


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
        if target.mode == "config_row":
            entries = _existing_list(manifest_dict, field)
            modules = set(target.values)
            kept = [entry for entry in entries if entry.get("module") not in modules]
            if len(kept) != len(entries):
                manifest_dict[field] = kept
                changed = True
        elif target.mode in ("module_list", "package_list"):
            entries = _existing_list(manifest_dict, field)
            values = set(target.values)
            kept = [value for value in entries if value not in values]
            if len(kept) != len(entries):
                manifest_dict[field] = kept
                changed = True
        elif target.mode == "mcp_entry":
            entries = _existing_list(manifest_dict, field)
            titles = {payload["title"] for payload in target.values}
            kept = [entry for entry in entries if entry.get("title") not in titles]
            if len(kept) != len(entries):
                manifest_dict[field] = kept
                changed = True
        elif target.mode == "descriptor_entry":
            entries = _existing_list(manifest_dict, field)
            ids = {payload["id"] for payload in target.values}
            kept = [entry for entry in entries if entry.get("id") not in ids]
            if len(kept) != len(entries):
                manifest_dict[field] = kept
                changed = True
        elif target.mode == "scalar_module":
            current = manifest_dict.get(field)
            if current is not None and current in target.values:
                manifest_dict[field] = None
                changed = True
    return changed
