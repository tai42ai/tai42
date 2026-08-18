"""Plugin routes survive an in-process epoch rebuild.

An epoch rebuild re-imports each manifest module under its mount binding to re-fire
its ``@custom_route`` registrations into the fresh route table. A plugin whose routes
register in a SIBLING of its manifest leaf (the channel-plugin shape) drops every
route unless the reload pops+reimports that sibling too — and a distribution shipping
TWO route modules under distinct bindings (the accounts-postgres shape) must have each
re-fired under ITS OWN binding only, never blanket-reloaded. A leaf that registers a
route ITSELF and also imports a route-carrying sibling records BOTH modules, and a
rebuild must re-fire each exactly once so both routes survive. These drive the real
importer + mount-binding + route-registry seams the reload path uses.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_contract.plugins import RouteDecl

from tai42_skeleton.app.importer import import_or_reload_package
from tai42_skeleton.app.mount_map import MountBinding, MountRegistrationError, bind_module
from tai42_skeleton.app.route_registry import RouteOwner, RouteRegistry

_PLUGIN_DIST_ROOT = Path(__file__).parent / "_fixtures" / "_plugin_dist"

# The sibling-route fixture (channel shape): the manifest leaf imports the sibling.
_SIBLING_LEAF = "route_sibling_plugin.register"
_SIBLING_MODULE = "route_sibling_plugin.inbound"
_SIBLING_OWNER = RouteOwner(kind="plugin", owner_ref="fixture/web", item_name="web")
_SIBLING_BINDING = MountBinding(
    owner_ref="fixture/web",
    item_name="web",
    base="channels/web",
    declared_routes=(RouteDecl(path="/inbound", methods=["POST"], public=True),),
)
_SIBLING_PATH = "/api/channels/web/inbound"

# The multi-router fixture (accounts-postgres shape): two leaves, two bindings.
_LOGIN_LEAF = "multi_router_plugin.routes_login"
_LOGIN_OWNER = RouteOwner(kind="plugin", owner_ref="fixture/accounts", item_name="login")
_LOGIN_BINDING = MountBinding(
    owner_ref="fixture/accounts",
    item_name="login",
    base="login",
    declared_routes=(RouteDecl(path="/session", methods=["POST"], public=True),),
)
_LOGIN_PATH = "/api/login/session"

_USERS_LEAF = "multi_router_plugin.routes_users"
_USERS_OWNER = RouteOwner(kind="plugin", owner_ref="fixture/accounts", item_name="users")
_USERS_BINDING = MountBinding(
    owner_ref="fixture/accounts",
    item_name="users",
    base="auth",
    declared_routes=(RouteDecl(path="/me", methods=["GET"], public=True),),
)
_USERS_PATH = "/api/auth/me"

# The dual-route fixture: the manifest leaf registers a route ITSELF and imports a
# route-carrying sibling, so the owner records BOTH modules under one binding.
_DUAL_LEAF = "dual_route_plugin.register"
_DUAL_SIBLING = "dual_route_plugin.inbound"
_DUAL_OWNER = RouteOwner(kind="plugin", owner_ref="fixture/dual", item_name="dual")
_DUAL_BINDING = MountBinding(
    owner_ref="fixture/dual",
    item_name="dual",
    base="dual",
    declared_routes=(
        RouteDecl(path="/status", methods=["GET"], public=True),
        RouteDecl(path="/inbound", methods=["POST"], public=True),
    ),
)
_DUAL_STATUS_PATH = "/api/dual/status"
_DUAL_INBOUND_PATH = "/api/dual/inbound"


def _forget(prefixes: tuple[str, ...]) -> None:
    for name in [n for n in sys.modules if any(n == p or n.startswith(f"{p}.") for p in prefixes)]:
        del sys.modules[name]


@pytest.fixture
def plugin_dist(monkeypatch: pytest.MonkeyPatch) -> Iterator[RouteRegistry]:
    """Put the fixture distributions on ``sys.path``, route their registrations into a
    FRESH registry (read indirectly by the probe), and forget the fixture modules and
    the probe before and after so each test imports them cleanly."""
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.route_registry.route_registry", registry)
    monkeypatch.syspath_prepend(str(_PLUGIN_DIST_ROOT))
    prefixes = ("route_sibling_plugin", "multi_router_plugin", "dual_route_plugin", "_regprobe")
    _forget(prefixes)
    try:
        yield registry
    finally:
        _forget(prefixes)


def _reload_extra(registry: RouteRegistry, owner: RouteOwner, leaf: str) -> frozenset[str]:
    """The sibling module(s) the reload pops for ``owner`` — computed exactly as
    ``_import_additive_plugin`` does (the owner's recorded route modules, minus the
    leaf itself)."""
    return registry.owner_route_modules(owner) - {leaf}


def test_sibling_route_survives_a_leaf_reimport(plugin_dist: RouteRegistry) -> None:
    registry = plugin_dist

    # Boot: importing the leaf under its binding imports the sibling fresh, so the route
    # registers and the owner's route-registering module is remembered as the SIBLING.
    with bind_module(_SIBLING_BINDING):
        import_or_reload_package(_SIBLING_LEAF)
    assert registry.match(_SIBLING_PATH, "POST") is not None
    assert registry.owner_route_modules(_SIBLING_OWNER) == frozenset({_SIBLING_MODULE})

    # A leaf-only re-import leaves the sibling cached in sys.modules, so its registration
    # never re-fires: the declared route goes unregistered and the bind-time completeness
    # check must raise — the failure a rebuild that reimported the leaf alone would hit,
    # quarantining the plugin and dropping every one of its routes from the epoch.
    registry.reset_shape_index()
    with pytest.raises(MountRegistrationError, match="never registered"), bind_module(_SIBLING_BINDING):
        import_or_reload_package(_SIBLING_LEAF)

    # Popping the owner's recorded sibling too re-fires it under the SAME binding: the
    # declared route re-registers and the route is back in the table the rebuilt epoch
    # serves. Per-owner scoping keeps the reimport to exactly this sibling.
    registry.reset_shape_index()
    extra = _reload_extra(registry, _SIBLING_OWNER, _SIBLING_LEAF)
    assert extra == frozenset({_SIBLING_MODULE})
    with bind_module(_SIBLING_BINDING):
        import_or_reload_package(_SIBLING_LEAF, extra)
    assert registry.match(_SIBLING_PATH, "POST") is not None


def test_multi_router_distribution_reloads_each_module_under_its_own_binding(plugin_dist: RouteRegistry) -> None:
    import _regprobe  # pyright: ignore[reportMissingImports]  (on sys.path via the plugin_dist fixture)

    registry = plugin_dist

    # Boot: each leaf is its OWN route module, imported under its OWN binding.
    with bind_module(_LOGIN_BINDING):
        import_or_reload_package(_LOGIN_LEAF)
    with bind_module(_USERS_BINDING):
        import_or_reload_package(_USERS_LEAF)
    assert registry.match(_LOGIN_PATH, "POST") is not None
    assert registry.match(_USERS_PATH, "GET") is not None

    # A self-registering leaf records only ITSELF, so its reload widens to NOTHING —
    # the crux that keeps the two bindings from crossing.
    assert _reload_extra(registry, _LOGIN_OWNER, _LOGIN_LEAF) == frozenset()
    assert _reload_extra(registry, _USERS_OWNER, _USERS_LEAF) == frozenset()

    registry.reset_shape_index()
    _regprobe.exec_count.clear()

    # A full epoch rebuild re-imports EVERY manifest module — here both leaves, each
    # under ITS OWN binding.
    with bind_module(_LOGIN_BINDING):
        import_or_reload_package(_LOGIN_LEAF, _reload_extra(registry, _LOGIN_OWNER, _LOGIN_LEAF))
    with bind_module(_USERS_BINDING):
        import_or_reload_package(_USERS_LEAF, _reload_extra(registry, _USERS_OWNER, _USERS_LEAF))

    # Both routes re-fired at their CORRECT bases, and each module ran EXACTLY once — its
    # own reload — never dragged into the other's. Nothing ran bare or under the wrong
    # binding. Per-owner scoping forbids login's reload from ALSO popping+reimporting
    # users: a whole-distribution widen would re-run users a second time under login's
    # binding (recording /api/login/me) or bare (raising).
    assert registry.match(_LOGIN_PATH, "POST") is not None
    assert registry.match(_USERS_PATH, "GET") is not None
    assert registry.match("/api/login/me", "GET") is None
    assert _regprobe.exec_count[_LOGIN_LEAF] == 1
    assert _regprobe.exec_count[_USERS_LEAF] == 1


def test_dual_route_leaf_and_sibling_both_survive_a_rebuild(plugin_dist: RouteRegistry) -> None:
    import _regprobe  # pyright: ignore[reportMissingImports]  (on sys.path via the plugin_dist fixture)

    registry = plugin_dist

    # Boot: the leaf registers its own route AND imports the sibling, so the owner
    # records BOTH modules and both routes serve.
    with bind_module(_DUAL_BINDING):
        import_or_reload_package(_DUAL_LEAF)
    assert registry.match(_DUAL_STATUS_PATH, "GET") is not None
    assert registry.match(_DUAL_INBOUND_PATH, "POST") is not None
    assert registry.owner_route_modules(_DUAL_OWNER) == frozenset({_DUAL_LEAF, _DUAL_SIBLING})

    # The reload extra is the sibling alone — the owner's route modules minus the leaf.
    extra = _reload_extra(registry, _DUAL_OWNER, _DUAL_LEAF)
    assert extra == frozenset({_DUAL_SIBLING})

    registry.reset_shape_index()
    _regprobe.exec_count.clear()

    # A rebuild pops+reimports the leaf and its sibling extra under the one binding: both
    # modules re-fire EXACTLY once and both routes are back in the table the epoch serves.
    with bind_module(_DUAL_BINDING):
        import_or_reload_package(_DUAL_LEAF, extra)
    assert registry.match(_DUAL_STATUS_PATH, "GET") is not None
    assert registry.match(_DUAL_INBOUND_PATH, "POST") is not None
    assert _regprobe.exec_count[_DUAL_LEAF] == 1
    assert _regprobe.exec_count[_DUAL_SIBLING] == 1
