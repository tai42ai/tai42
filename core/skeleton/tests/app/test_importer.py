"""Importer coverage: ``import_or_reload_package`` (empty/happy paths, the
raise-on-missing-package and raise-on-broken-submodule paths) and the
``_stable_cycle_fallback`` ordering.
"""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

from tai42_skeleton.app.importer import (
    _stable_cycle_fallback,
    import_or_reload_package,
)


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
    # Hold the probe by the same absolute dotted name the importer resolves the
    # manifest package under, so this is the exact module ``side_effect_pkg``'s
    # __init__ appends to. A relative import would instead bind the probe under
    # whatever package name pytest derived for THIS test module, which is the
    # rootdir-relative ``core.skeleton.tests.app._fixtures.counter_probe`` in a
    # whole-repo run and ``tests.app._fixtures.counter_probe`` in a skeleton-only
    # run — a different object whose list never sees the side effect.
    counter_probe = importlib.import_module("tests.app._fixtures.counter_probe")

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


def test_extra_modules_are_popped_and_reimported_with_the_leaf():
    # A bound plugin's route-registering SIBLING is passed as an extra so it re-fires
    # on reload: it is popped+reimported alongside the leaf, not left cached.
    leaf = "tests.app._fixtures.neutral.leaf"
    sibling = "tests.app._fixtures.neutral.leaf2"
    import_or_reload_package(leaf)  # boot both into the cache
    importlib.import_module(sibling)
    reloaded = import_or_reload_package(leaf, [sibling])
    assert sibling in reloaded
    assert sys.modules[sibling].VALUE == 2


def test_stale_extra_module_is_dropped_not_raised(caplog):
    # A plugin update that renamed its route module leaves a STALE extra that no longer
    # resolves — it is dropped (the leaf's own import pulls the current module), never
    # raised, and the drop is logged rather than passed silently.
    leaf = "tests.app._fixtures.neutral.leaf"
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.app.importer"):
        reloaded = import_or_reload_package(leaf, ["tests.app._fixtures.neutral.gone_module"])
    assert reloaded == [leaf]
    assert any(
        "dropping stale route-sibling extra tests.app._fixtures.neutral.gone_module" in record.message
        for record in caplog.records
    )


def test_extra_whose_backing_file_was_deleted_is_dropped_not_raised(tmp_path, monkeypatch, caplog):
    # A plugin UPDATE can delete a route-sibling whose module is STILL cached in
    # ``sys.modules`` (imported under the old epoch). ``find_spec`` short-circuits to the
    # cached module's stale ``__spec__``, so the staleness check would keep the vanished
    # module, pop it, and fail to reimport it — a ModuleNotFoundError that quarantines the
    # plugin. The extra must instead be dropped: popped first, so ``find_spec`` resolves
    # against the filesystem, sees the deleted file, and drops it with the log.
    pkg = tmp_path / "deleted_sibling_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "leaf.py").write_text("MARKER = 'leaf'\n")
    (pkg / "sibling.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    leaf = "deleted_sibling_pkg.leaf"
    sibling = "deleted_sibling_pkg.sibling"
    try:
        import_or_reload_package(leaf)  # boot the leaf
        importlib.import_module(sibling)  # cache the sibling under the old epoch
        assert sibling in sys.modules
        # The update removes the sibling's backing file while its module stays cached.
        (pkg / "sibling.py").unlink()

        with caplog.at_level(logging.INFO, logger="tai42_skeleton.app.importer"):
            reloaded = import_or_reload_package(leaf, [sibling])

        # No raise: the vanished sibling is dropped, and the leaf reimport proceeds.
        assert reloaded == [leaf]
        assert sibling not in sys.modules
        assert any(f"dropping stale route-sibling extra {sibling}" in record.message for record in caplog.records)
    finally:
        sys.modules.pop(leaf, None)
        sys.modules.pop(sibling, None)
        sys.modules.pop("deleted_sibling_pkg", None)


def test_stable_cycle_fallback_orders_by_depth_then_name():
    nodes = {"a.b.c", "a", "a.b", "z"}
    assert _stable_cycle_fallback(nodes) == ["a", "z", "a.b", "a.b.c"]
