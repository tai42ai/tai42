"""Importer coverage: ``import_or_reload_package`` (empty/happy paths, the
raise-on-missing-package and raise-on-broken-submodule paths) and the
``_stable_cycle_fallback`` ordering.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from tai42_skeleton.app.importer import (
    _stable_cycle_fallback,
    import_or_reload_package,
)

# A standalone top-level fixture distribution whose manifest leaf
# (``route_sibling_plugin.register``) registers its HTTP route via an ``import
# route_sibling_plugin.inbound`` side-effect — the sibling-route shape the reload bug
# hit. Its parent dir is put on ``sys.path`` so the whole-distribution widen can reach
# it, and a synthetic ``dist_map`` maps its top-level package to a distribution.
_PLUGIN_DIST_ROOT = Path(__file__).parent / "_fixtures" / "_plugin_dist"
_SIBLING_MANIFEST_LEAF = "route_sibling_plugin.register"
_SIBLING_DIST_MAP = {"route_sibling_plugin": ["route-sibling-plugin"]}


def test_empty_name_returns_empty():
    assert import_or_reload_package("") == []


def test_happy_path_reimports_package_and_submodules():
    reloaded = import_or_reload_package("tests.app._fixtures.neutral")
    assert "tests.app._fixtures.neutral" in reloaded
    assert "tests.app._fixtures.neutral.leaf" in reloaded
    # The modules are live in ``sys.modules`` after the reload.
    assert sys.modules["tests.app._fixtures.neutral.leaf"].MARKER == "leaf"


def test_single_module_reload_has_no_submodules():
    # A plain module (no ``submodule_search_locations``) discovers only itself —
    # the false branch of the package-walk guard.
    reloaded = import_or_reload_package("tests.app._fixtures.neutral.leaf2")
    assert reloaded == ["tests.app._fixtures.neutral.leaf2"]
    assert sys.modules["tests.app._fixtures.neutral.leaf2"].VALUE == 2


def test_missing_top_level_package_raises():
    # A manifest-named package that cannot be found is corrupt configuration
    # and must abort loudly, naming the package.
    with pytest.raises(ImportError, match="Cannot find module totally_bogus_pkg_xyz"):
        import_or_reload_package("totally_bogus_pkg_xyz")


def test_failing_submodule_raises_with_module_name(monkeypatch):
    # Simulate one submodule failing to import (without a real import-time error
    # in a fixture, which would also fail collection): the importer raises,
    # naming the broken module and chaining the original error.
    real_import = importlib.import_module

    def flaky_import(name, *args, **kwargs):
        if name == "tests.app._fixtures.neutral.leaf2":
            raise ImportError("simulated import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", flaky_import)
    with pytest.raises(ImportError, match=r"Failed to import module tests\.app\._fixtures\.neutral\.leaf2") as ei:
        import_or_reload_package("tests.app._fixtures.neutral")
    assert isinstance(ei.value.__cause__, ImportError)


def test_side_effecting_init_runs_exactly_once():
    # The package __init__ has a side effect (appends to an out-of-tree log).
    # Discovery must enumerate submodules WITHOUT importing the package, so the
    # pop+reimport step is the ONLY import — the __init__ runs exactly once.
    # ``walk_packages`` would import the package to recurse, then reimport it,
    # running the __init__ twice (the double-register bug).
    from tests.app._fixtures import counter_probe

    counter_probe.INIT_CALLS.clear()
    reloaded = import_or_reload_package("tests.app._fixtures.side_effect_pkg")

    assert counter_probe.INIT_CALLS == ["side_effect_pkg"]
    # The submodule was discovered (without importing the package to find it).
    assert "tests.app._fixtures.side_effect_pkg.child" in reloaded


def test_reloading_a_provider_registering_module_is_reload_safe():
    # A plugin whose module body calls register_accounts_provider is reloaded by
    # import_or_reload_package (pop + re-execute), exactly as boot/reload does.
    # Before the reload-safe registry fix the SECOND reload re-ran the module-level
    # registration and raised ValueError("... already registered"), crashing boot;
    # now the re-registration of the same declared provider is a no-op.
    from tai42_contract.access_control.registry import get_identity_provider_factory
    from tai42_contract.access_control.registry import reset_registry as reset_identity
    from tai42_contract.accounts.registry import get_accounts_provider_factory
    from tai42_contract.accounts.registry import reset_registry as reset_accounts

    reset_accounts()
    reset_identity()
    try:
        import_or_reload_package("tests.app._fixtures.accounts_reg")  # first import: registers
        import_or_reload_package("tests.app._fixtures.accounts_reg")  # reload: was the crash
        # Registered in BOTH registries and still resolvable after the reload.
        assert get_accounts_provider_factory("fixture-accounts") is not None
        assert get_identity_provider_factory("fixture-accounts") is not None
    finally:
        reset_accounts()
        reset_identity()
        sys.modules.pop("tests.app._fixtures.accounts_reg", None)


def test_stable_cycle_fallback_orders_by_depth_then_name():
    nodes = {"a.b.c", "a", "a.b", "z"}
    assert _stable_cycle_fallback(nodes) == ["a", "z", "a.b", "a.b.c"]


@pytest.fixture
def sibling_route_plugin(monkeypatch: pytest.MonkeyPatch):
    """Make the sibling-route fixture distribution importable and route its route
    registrations into a FRESH registry, cleaning both up after the test."""
    from tai42_skeleton.app.route_registry import RouteRegistry

    registry = RouteRegistry()
    # The fixture's ``inbound`` reads the registry indirectly off the module, so this
    # swap redirects its registration into the fresh instance.
    monkeypatch.setattr("tai42_skeleton.app.route_registry.route_registry", registry)
    monkeypatch.syspath_prepend(str(_PLUGIN_DIST_ROOT))
    for name in [n for n in sys.modules if n == "route_sibling_plugin" or n.startswith("route_sibling_plugin.")]:
        del sys.modules[name]
    try:
        yield registry
    finally:
        for name in [n for n in sys.modules if n == "route_sibling_plugin" or n.startswith("route_sibling_plugin.")]:
            del sys.modules[name]


def test_reload_refires_a_route_registering_sibling_of_the_manifest_leaf(sibling_route_plugin):
    # A plugin whose route lives in a SIBLING of its manifest leaf (the twilio/whatsapp/
    # web/slack/telegram channel shape). Boot imports the leaf, which imports the sibling
    # fresh, so the route registers. On a reload the epoch rebuild clears the staged route
    # table and re-imports the manifest leaf — and the route must come back.
    registry = sibling_route_plugin
    path, method = "/api/channels/fixture/inbound", "POST"

    # Boot: fresh import records the sibling's route.
    import_or_reload_package(_SIBLING_MANIFEST_LEAF, _SIBLING_DIST_MAP)
    assert registry.match(path, method) is not None

    # The epoch rebuild clears the route table before the re-import re-populates it.
    registry.reset_shape_index()
    assert registry.match(path, method) is None

    # OLD leaf-only behavior (no dist_map): re-importing ONLY the leaf leaves the sibling
    # cached in sys.modules, so its registration never re-fires — the route stays dropped
    # (the silent-unmount bug). This assertion is RED against the pre-fix reload path.
    import_or_reload_package(_SIBLING_MANIFEST_LEAF)
    assert registry.match(path, method) is None

    # THE FIX: widening the reload to the plugin's whole distribution (dist_map-derived)
    # pops+reimports the sibling too, so its route registration re-fires and the route is
    # back in the table the rebuilt epoch will serve.
    registry.reset_shape_index()
    import_or_reload_package(_SIBLING_MANIFEST_LEAF, _SIBLING_DIST_MAP)
    assert registry.match(path, method) is not None
