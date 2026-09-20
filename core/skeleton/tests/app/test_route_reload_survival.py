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
from starlette.routing import Route
from tai42_contract.app import tai42_app
from tai42_contract.plugins import RouteDecl

from tai42_skeleton.app.importer import import_or_reload_package
from tai42_skeleton.app.mount_map import MountBinding, MountRegistrationError, bind_module
from tai42_skeleton.app.route_registry import RouteOwner, RouteRegistry
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.marketplace.compat import CompatVerdict

_PLUGIN_DIST_ROOT = Path(__file__).parent / "_fixtures" / "_plugin_dist"

# A no-distribution compat verdict: the fixture packages are on ``sys.path`` but not pip
# installed, so the role loops proceed with a logged note rather than a dist-backed range.
_UNKNOWN_VERDICT = CompatVerdict("unknown", "no dist")

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


_EPS_OWNER = RouteOwner(kind="plugin", owner_ref="fixture/eps", item_name="eps")
_EPS_BINDING = MountBinding(
    owner_ref="fixture/eps",
    item_name="eps",
    base="eps",
    declared_routes=(RouteDecl(path="/inbound", methods=["POST"], public=True),),
)
_EPS_PATH = "/api/eps/inbound"

_EPS_INBOUND_SRC = """\
from _regprobe import register_route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import RouteOwner

OWNER = RouteOwner(kind="plugin", owner_ref="fixture/eps", item_name="eps")


async def inbound(request: Request) -> Response:
    return JSONResponse({"data": {}})


register_route("/inbound", "POST", OWNER, inbound)
"""

# v2 leaf: the sibling is gone, so the leaf registers the route ITSELF.
_EPS_V2_LEAF_SRC = """\
from _regprobe import register_route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import RouteOwner

OWNER = RouteOwner(kind="plugin", owner_ref="fixture/eps", item_name="eps")


async def inbound(request: Request) -> Response:
    return JSONResponse({"data": {}})


register_route("/inbound", "POST", OWNER, inbound)
"""


def test_update_that_deletes_the_sibling_reloads_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The epsilon-shaped plugin UPDATE: v1's manifest leaf registers by importing a
    # route-carrying SIBLING; v2's leaf self-registers and the update REMOVES the sibling
    # file. The reload's extra set — the owner's route modules recorded on the OLD epoch —
    # still names the vanished sibling, whose module is still cached in ``sys.modules``.
    # The reload must drop that extra against the filesystem (not its stale cached spec),
    # so the leaf reimport re-fires v2's own registration: the route stays live, the
    # plugin is not quarantined by a ModuleNotFoundError, and the audit sees no drop.
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.route_registry.route_registry", registry)
    monkeypatch.syspath_prepend(str(_PLUGIN_DIST_ROOT))  # for ``_regprobe``
    monkeypatch.syspath_prepend(str(tmp_path))

    pkg = tmp_path / "epsilon_plugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "register.py").write_text("import epsilon_plugin.inbound  # noqa: F401\n")
    (pkg / "inbound.py").write_text(_EPS_INBOUND_SRC)

    leaf = "epsilon_plugin.register"
    sibling = "epsilon_plugin.inbound"
    _forget(("epsilon_plugin",))
    try:
        # Boot v1: the leaf imports the sibling, which registers the route; the owner's
        # recorded route module is the SIBLING.
        with bind_module(_EPS_BINDING):
            import_or_reload_package(leaf)
        assert registry.match(_EPS_PATH, "POST") is not None
        assert registry.owner_route_modules(_EPS_OWNER) == frozenset({sibling})

        # The reload extra is computed from the OLD epoch — the vanished sibling.
        extra = _reload_extra(registry, _EPS_OWNER, leaf)
        assert extra == frozenset({sibling})

        # The update lands: the leaf self-registers and the sibling file is DELETED, while
        # its module stays cached in ``sys.modules`` from the boot import.
        (pkg / "register.py").write_text(_EPS_V2_LEAF_SRC)
        (pkg / "inbound.py").unlink()
        assert sibling in sys.modules

        # Reload under the binding with the stale extra: the sibling is dropped (its file is
        # gone), the leaf re-fires v2's own registration, and the route is back — no
        # ModuleNotFoundError, no ``MountRegistrationError`` quarantine.
        registry.reset_shape_index()
        with bind_module(_EPS_BINDING):
            reloaded = import_or_reload_package(leaf, extra)
        assert reloaded == [leaf]
        assert sibling not in sys.modules
        assert registry.match(_EPS_PATH, "POST") is not None
        # The self-registering leaf now carries the route.
        assert leaf in registry.owner_route_modules(_EPS_OWNER)
    finally:
        _forget(("epsilon_plugin",))


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


def test_channel_walk_under_a_bound_leaf_is_unchanged_by_the_mount_map(plugin_dist: RouteRegistry) -> None:
    # The channel shape driven exactly as ``_import_additive_plugin`` does — an OUTER
    # ``bind_module`` for the manifest leaf PLUS the mount map — must be unchanged by the
    # per-submodule resolution: the leaf's own binding equals the active one, so it is not
    # re-bound, and the route-carrying sibling (absent from the map) keeps the leaf's
    # binding and registers the plugin's route.
    registry = plugin_dist
    mount_map = {_SIBLING_LEAF: _SIBLING_BINDING}
    with bind_module(_SIBLING_BINDING):
        import_or_reload_package(_SIBLING_LEAF, mount_map=mount_map)
    assert registry.match(_SIBLING_PATH, "POST") is not None
    assert registry.owner_route_modules(_SIBLING_OWNER) == frozenset({_SIBLING_MODULE})


def test_multi_router_package_walk_binds_each_submodule_under_its_own_binding(plugin_dist: RouteRegistry) -> None:
    # The accounts-postgres shape wired under a NON-route role: the manifest names the ROOT
    # package, whose walk carries NO outer binding. Each route submodule the map binds must
    # register under ITS OWN binding — never bare (which raises) and never under a sibling's
    # binding — with each module executed exactly once.
    import _regprobe  # pyright: ignore[reportMissingImports]  (on sys.path via the plugin_dist fixture)

    registry = plugin_dist
    mount_map = {_LOGIN_LEAF: _LOGIN_BINDING, _USERS_LEAF: _USERS_BINDING}
    _regprobe.exec_count.clear()

    reloaded = import_or_reload_package("multi_router_plugin", mount_map=mount_map)

    assert "multi_router_plugin" in reloaded
    assert registry.match(_LOGIN_PATH, "POST") is not None
    assert registry.match(_USERS_PATH, "GET") is not None
    # Neither submodule ran under the other's binding (that would record /api/login/me).
    assert registry.match("/api/login/me", "GET") is None
    assert _regprobe.exec_count[_LOGIN_LEAF] == 1
    assert _regprobe.exec_count[_USERS_LEAF] == 1


_FOREIGN_ROUTES_LEAF = "foreign_role_plugin.routes"
_FOREIGN_OWNER = RouteOwner(kind="plugin", owner_ref="fixture/accounts", item_name="login")
_FOREIGN_ROUTES_BINDING = MountBinding(
    owner_ref="fixture/accounts",
    item_name="login",
    base="login",
    declared_routes=(RouteDecl(path="/session", methods=["POST"], public=True),),
)
_FOREIGN_ROUTES_PATH = "/api/login/session"


@pytest.fixture
def foreign_role_dist(monkeypatch: pytest.MonkeyPatch) -> Iterator[RouteRegistry]:
    """Put the foreign-role fixture distribution on ``sys.path``, route its ``custom_route``
    registrations into a FRESH registry, and forget its modules before and after."""
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.http.route_registry", registry)
    monkeypatch.syspath_prepend(str(_PLUGIN_DIST_ROOT))
    _forget(("foreign_role_plugin",))
    try:
        yield registry
    finally:
        _forget(("foreign_role_plugin",))


def test_package_walk_under_foreign_role_binds_a_mapped_route_submodule(foreign_role_dist: RouteRegistry) -> None:
    # A package listed under a NON-route role: importing it runs the ``provider`` side-effect
    # AND the walk sweeps in ``routes`` — a mapped route submodule that captures
    # ``mount_base()`` and registers its route at import. The walk carries no binding, so
    # ``routes`` is bound from the mount map by its OWN name: the import succeeds, its
    # ``mount_base()`` resolves to its declared base, and its route registers under its
    # own binding.
    registry = foreign_role_dist
    app = TaiMCP(name="foreign-role-walk")
    mount_map = {_FOREIGN_ROUTES_LEAF: _FOREIGN_ROUTES_BINDING}

    with tai42_app.bound(app):
        reloaded = import_or_reload_package("foreign_role_plugin", mount_map=mount_map)

    provider = sys.modules["foreign_role_plugin.provider"]
    routes = sys.modules["foreign_role_plugin.routes"]
    assert provider.REGISTERED is True
    assert set(reloaded) >= {"foreign_role_plugin", "foreign_role_plugin.provider", _FOREIGN_ROUTES_LEAF}
    # ``mount_base()`` captured at import resolved to the submodule's own declared base.
    assert routes.MOUNT_BASE == "/api/login"
    meta = registry.match(_FOREIGN_ROUTES_PATH, "POST")
    assert meta is not None
    assert meta.owner == _FOREIGN_OWNER
    assert meta.public is True


def test_unmapped_route_submodule_in_a_foreign_walk_still_raises(foreign_role_dist: RouteRegistry) -> None:
    # The loud-failure floor: a route submodule the walk reaches with NEITHER its own
    # binding in the map NOR any active binding calls ``mount_base()`` at import and must
    # still raise exactly as today, quarantining the walked package rather than booting a
    # silently mis-mounted route.
    app = TaiMCP(name="foreign-role-unmapped")
    with tai42_app.bound(app), pytest.raises(MountRegistrationError, match="no mount binding present"):
        import_or_reload_package("foreign_role_plugin", mount_map={})


def _fastmcp_route_paths(app: TaiMCP) -> list[str]:
    return [route.path for route in app._fast_mcp._additional_http_routes if isinstance(route, Route)]


def test_accounts_shape_runs_each_route_module_body_exactly_once_per_pass(
    plugin_dist: RouteRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The accounts-postgres shape: the ROOT package is a lifecycle entry AND its two route
    # submodules are their own router entries. Driven through the REAL role-loop dedup
    # (``_import_additive_plugin`` with the pass's run-once ledger), the lifecycle walk runs
    # each route module body once under its own binding, and the router-role import — reaching
    # already-executed modules — does NOT re-run them. Exactly one execution per module per
    # pass, all routes registered, completeness green (no MountRegistrationError). A second
    # pass with a FRESH ledger re-executes cleanly, proving the ledger is per-pass.
    import _regprobe  # pyright: ignore[reportMissingImports]  (on sys.path via the plugin_dist fixture)

    registry = plugin_dist
    monkeypatch.setattr("tai42_skeleton.marketplace.compat.module_compat", lambda module, dist_map: _UNKNOWN_VERDICT)
    app = TaiMCP(name="accounts-shape")
    app._mount_map = {_LOGIN_LEAF: _LOGIN_BINDING, _USERS_LEAF: _USERS_BINDING}

    def _one_pass() -> None:
        _regprobe.exec_count.clear()
        registry.reset_shape_index()
        executed: set[str] = set()
        # Lifecycle loop: the root package sweeps in both route submodules under their bindings.
        assert app._import_additive_plugin("multi_router_plugin", "lifecycle", {}, executed) is True
        # Router role: each route submodule is its own entry — already executed by the walk.
        assert app._import_additive_plugin(_LOGIN_LEAF, "router", {}, executed) is True
        assert app._import_additive_plugin(_USERS_LEAF, "router", {}, executed) is True
        # The walk executed the submodules and the ledger recorded them, so the router-role import skipped.
        assert {_LOGIN_LEAF, _USERS_LEAF} <= executed
        assert _regprobe.exec_count[_LOGIN_LEAF] == 1
        assert _regprobe.exec_count[_USERS_LEAF] == 1
        assert registry.match(_LOGIN_PATH, "POST") is not None
        assert registry.match(_USERS_PATH, "GET") is not None

    _one_pass()
    # A second reload pass re-executes cleanly with a fresh ledger — no leak across passes.
    _one_pass()


def test_foreign_walk_submodule_failure_rolls_back_symmetrically(foreign_role_dist: RouteRegistry) -> None:
    # A route submodule swept in by a foreign-role walk whose binding declares a route it
    # never registers fails the bind-time completeness check. Because the walk newly binds
    # it, the importer guards it with the same savepoint/rollback the own-role import uses:
    # the row it DID commit is rolled back before the fault propagates, so a failed foreign
    # walk leaves NO half-registered state — symmetric with a first-import failure today.
    registry = foreign_role_dist
    app = TaiMCP(name="foreign-role-rollback")
    binding = MountBinding(
        owner_ref="fixture/accounts",
        item_name="login",
        base="login",
        declared_routes=(
            RouteDecl(path="/session", methods=["POST"], public=True),
            RouteDecl(path="/never", methods=["GET"], public=True),
        ),
    )
    mount_map = {_FOREIGN_ROUTES_LEAF: binding}

    with (
        tai42_app.bound(app),
        pytest.raises(MountRegistrationError, match="never registered"),
    ):
        import_or_reload_package(
            "foreign_role_plugin",
            mount_map=mount_map,
            route_savepoint=app._http_surface.route_table_savepoint,
            route_rollback=app._http_surface.rollback_module_routes,
        )

    # The /session row the module committed before the completeness fault is gone from BOTH
    # the metadata registry and the served FastMCP route table.
    assert registry.match(_FOREIGN_ROUTES_PATH, "POST") is None
    assert _FOREIGN_ROUTES_PATH not in _fastmcp_route_paths(app)
