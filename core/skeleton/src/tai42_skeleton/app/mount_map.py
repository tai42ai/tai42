"""The boot/reload MOUNT MAP: module → its declared HTTP mount.

Built once per registration pass at the single import-driver seam every process
door crosses (``TaiMCPLifecycleMixin._initialize_components``), BEFORE the
manifest's channel/router modules are imported. While a bound module imports, a
contextvar carries its :class:`MountBinding` so ``HttpSurface.custom_route``
resolves each declared route to its absolute path and public flag from the
declaration instead of trusting the module's own arguments.

Binding sources, in order:

* every ``marketplace_installs`` row's stored spec — its route-carrying items,
  each mounted at the row's persisted ``route_mounts[item_name]`` override or the
  declaration's ``base``;
* every manifest-listed channel/router module NOT covered by an install row —
  located through its distribution's packaged ``tai-plugin.yml``. A module that
  maps to a route-DECLARING item binds at the declaration's base; a module whose
  matching item declares NO routes binds a ``forbidden`` marker (any
  ``custom_route`` from it is a loud error); a module with no packaged
  ``tai-plugin.yml`` at all (an operator-authored deployment module) gets no
  binding and keeps core semantics.

A resolved PUBLIC route under any reserved prefix fails the map build loudly.
"""

from __future__ import annotations

import contextlib
import importlib.resources
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field

from tai42_contract.plugins import PluginItem, PluginItemKind, PluginSpec, RouteDecl
from tai42_kit.plugins import PLUGIN_SPEC_FILENAME, parse_plugin_spec

from tai42_skeleton.access_control.path_canon import canonicalize_path

# The item kinds whose module registers HTTP routes: a router always declares
# routes, a channel optionally does. Every other kind never mounts a route.
_ROUTE_CARRYING_KINDS = frozenset({PluginItemKind.CHANNEL, PluginItemKind.ROUTER})


class MountMapError(RuntimeError):
    """A mount-map build fault that must fail the boot/reload loudly — a resolved
    public route under a reserved prefix."""


class MountRegistrationError(RuntimeError):
    """A ``custom_route`` call that violates its module's mount binding — an
    undeclared route, an explicit ``authed`` on a declared route, a declared route
    that never registered, or any route from a route-less item's module."""


@dataclass(frozen=True)
class MountBinding:
    """One module's declared mount. ``forbidden`` marks a module whose item
    declares NO routes: any ``custom_route`` from it is a registration error.
    ``declared_routes`` is empty for a forbidden binding."""

    owner_ref: str
    item_name: str
    base: str
    declared_routes: tuple[RouteDecl, ...]
    forbidden: bool = False

    def resolved_path(self, path: str) -> str:
        """The absolute served path of a declared relative ``path``: the fixed
        ``/api/`` root, the mount ``base``, then the declared path."""
        return f"/api/{self.base}{path}"

    def find_route(self, path: str, methods: frozenset[str]) -> RouteDecl | None:
        """The declared row matching ``path`` with EXACTLY ``methods``, or ``None``."""
        for route in self.declared_routes:
            if route.path == path and frozenset(route.methods) == methods:
                return route
        return None


@dataclass
class _MountContext:
    binding: MountBinding
    seen: set[tuple[str, frozenset[str]]] = field(default_factory=set)


_current: ContextVar[_MountContext | None] = ContextVar("mount_binding", default=None)


def current_mount_binding() -> MountBinding | None:
    """The mount binding of the module importing on this coroutine/thread, or
    ``None`` off a bound import (a core router, an operator-authored module)."""
    ctx = _current.get()
    return ctx.binding if ctx is not None else None


def note_registered(path: str, methods: frozenset[str]) -> None:
    """Record that the importing module registered the declared row ``(path,
    methods)``, so the post-import completeness check knows it was served. A no-op
    off a bound import."""
    ctx = _current.get()
    if ctx is not None:
        ctx.seen.add((path, methods))


@contextlib.contextmanager
def bind_module(binding: MountBinding | None) -> Iterator[None]:
    """Carry ``binding`` for the span of one module import; a ``None`` binding is a
    plain pass-through (core semantics). On a clean import of a route-declaring
    binding, verify every declared row registered — a declared-but-unregistered
    row raises :class:`MountRegistrationError`."""
    if binding is None:
        yield
        return
    ctx = _MountContext(binding)
    token = _current.set(ctx)
    try:
        yield
    except BaseException:
        raise
    else:
        _verify_all_registered(ctx)
    finally:
        _current.reset(token)


def _verify_all_registered(ctx: _MountContext) -> None:
    declared = {(route.path, frozenset(route.methods)) for route in ctx.binding.declared_routes}
    missing = declared - ctx.seen
    if missing:
        rows = sorted(f"{'/'.join(sorted(methods))} {path}" for path, methods in missing)
        raise MountRegistrationError(
            f"plugin {ctx.binding.owner_ref!r} item {ctx.binding.item_name!r} declares route(s) in "
            f"tai-plugin.yml that its module never registered: {rows}"
        )


def build_mount_map(
    manifest_modules: Sequence[str],
    reserved_prefixes: Sequence[str],
    load_overrides: Callable[[], Mapping[str, Mapping[str, str]]],
) -> dict[str, MountBinding]:
    """Build the module → :class:`MountBinding` map for one registration pass.

    Each channel/router module this pass imports is resolved through its packaged
    ``tai-plugin.yml`` (the actual installed code's declaration): a route-DECLARING
    item binds at its declared base, a route-LESS item binds a ``forbidden`` marker,
    and a module shipping no spec (an operator-authored module) gets no binding.

    ``load_overrides`` yields the persisted ``{ref: {item_name: base}}`` route-mount
    overrides — it is invoked ONLY when at least one real plugin route module is
    present, so a deployment with none takes no install-store round-trip.
    ``reserved_prefixes`` are the never-public prefixes a resolved public route may
    not fall under.
    """
    resolved: dict[str, tuple[str, PluginItem]] = {}
    for module in manifest_modules:
        if module in resolved:
            continue
        spec = _packaged_spec_for_module(module)
        if spec is None:
            continue
        item = _route_item_for_module(spec, module)
        if item is None:
            continue
        resolved[module] = (spec.ref, item)

    overrides = load_overrides() if resolved else {}
    mapping = {module: _binding_for_item(ref, item, overrides.get(ref, {})) for module, (ref, item) in resolved.items()}
    _reject_reserved_public_routes(mapping, reserved_prefixes)
    return mapping


def _binding_for_item(ref: str, item: PluginItem, route_mounts: Mapping[str, str]) -> MountBinding:
    if item.routes is None:
        return MountBinding(owner_ref=ref, item_name=item.name, base="", declared_routes=(), forbidden=True)
    base = route_mounts.get(item.name, item.routes.base)
    return MountBinding(
        owner_ref=ref,
        item_name=item.name,
        base=base,
        declared_routes=tuple(item.routes.paths),
    )


def _route_item_for_module(spec: PluginSpec, module: str) -> PluginItem | None:
    for item in spec.provides:
        if item.kind in _ROUTE_CARRYING_KINDS and item.module == module:
            return item
    return None


def _packaged_spec_for_module(module: str) -> PluginSpec | None:
    """The ``PluginSpec`` packaged beside the module's top-level import package
    (``<top_level>/tai-plugin.yml``), or ``None`` when the package ships none (an
    operator-authored module)."""
    top_level = module.partition(".")[0]
    try:
        resource = importlib.resources.files(top_level).joinpath(PLUGIN_SPEC_FILENAME)
    except (ModuleNotFoundError, ImportError):
        return None
    if not resource.is_file():
        return None
    return parse_plugin_spec(resource.read_text(encoding="utf-8"), source=f"{top_level}/{PLUGIN_SPEC_FILENAME}")


def _reject_reserved_public_routes(mapping: Mapping[str, MountBinding], reserved_prefixes: Sequence[str]) -> None:
    offenders: list[str] = []
    for binding in mapping.values():
        if binding.forbidden:
            continue
        for route in binding.declared_routes:
            if not route.public:
                continue
            # Canonicalize (dot-resolution + slash-collapse) BEFORE the prefix test, so
            # "reserved always wins" holds against a non-canonical resolved path exactly
            # as the runtime verifier judges it — a base carrying a ``..`` cannot dodge
            # the reserved prefix by resolving under it only after normalization.
            resolved = canonicalize_path(binding.resolved_path(route.path))
            if any(resolved == prefix or resolved.startswith(f"{prefix}/") for prefix in reserved_prefixes):
                offenders.append(f"{binding.owner_ref}:{binding.item_name} {resolved}")
    if offenders:
        raise MountMapError(
            "declared public route(s) resolve under a reserved never-public prefix "
            f"{sorted(reserved_prefixes)}: {sorted(offenders)} — a public route may not mount there"
        )
