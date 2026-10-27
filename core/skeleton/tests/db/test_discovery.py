"""Chain discovery: resolving skeleton and plugin migration chains into runner entries.

No live Postgres — pure resolution against installed package metadata. The
skeleton and the ``tai42-skeleton`` distribution are guaranteed installed in the
test environment, so they double as a real fixture for the package-relative
resolution path. A configured default database (password only) lets the migrator
identity resolve without opening a connection.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import MigrationDiscoveryError

from tai42_skeleton.db import (
    SKELETON_COMPONENT,
    all_migration_entries,
    installed_plugin_entries,
    installed_plugin_entry,
)
from tai42_skeleton.db.discovery import skeleton_migrations_dir
from tai42_skeleton.marketplace.compat import distribution_map
from tai42_skeleton.marketplace.installed_packages import InstalledPackages


@pytest.fixture(autouse=True)
def _default_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default database is configured by a password alone; the migrator identity
    # then resolves (no connection is opened while building entries).
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")


def _spec(*, package: str, migrations: str | None) -> PluginSpec:
    return PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "tai42",
            "name": "widget",
            "package": package,
            "version": "1.0.0",
            "description": "A test plugin",
            "license": "Apache-2.0",
            "contract": ">=0.1,<1.0",
            "categories": ["dev"],
            "provides": [{"kind": "tool", "name": "w", "module": "w.m", "description": "d"}],
            "migrations": migrations,
        }
    )


def test_skeleton_migrations_dir_exists() -> None:
    assert skeleton_migrations_dir().is_dir()


def test_installed_entry_for_a_distribution_that_is_not_installed_is_loud() -> None:
    with pytest.raises(MigrationDiscoveryError, match="'tai42-no-such-plugin-xyz' is not installed"):
        installed_plugin_entry("tai42-no-such-plugin-xyz")


@pytest.mark.parametrize("name", ["tai42-skeleton", "Tai42_Skeleton"])
def test_installed_entry_for_a_distribution_packaging_no_spec_is_loud(name: str) -> None:
    # ``tai42-skeleton`` is installed (found under any spelling pip treats as the same
    # project) and packages no ``tai-plugin.yml``: no chain folder can be located for it.
    with pytest.raises(MigrationDiscoveryError, match=r"packages no tai-plugin\.yml"):
        installed_plugin_entry(name)


# --- the installed-distribution scan ---------------------------------------


_WIDGET_SPEC_YAML = """\
spec_version: 1
namespace: tai42
name: widget
package: tai42-widget
version: 1.0.0
description: An installed test plugin
license: Apache-2.0
contract: ">=0.1,<1.0"
categories: [dev]
provides:
  - kind: tool
    name: w
    module: w.m
    description: d
migrations: migrations
"""


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make the given site directories the whole environment: ``sys.path`` holds them alone.

    The modules discovery imports lazily are imported first, so narrowing ``sys.path`` only
    changes which distributions are installed. The plugins prefix is unset unless a test
    configures one.
    """
    import tai42_skeleton.marketplace.prefix
    import tai42_skeleton.states.db

    monkeypatch.setattr(tai42_skeleton.marketplace.prefix, "configured_prefix", lambda: None)

    def _set(*sites: Path) -> None:
        monkeypatch.setattr(sys, "path", [str(site) for site in sites])
        importlib.invalidate_caches()

    return _set


def _prefix_site(prefix: Path) -> Path:
    from tai42_skeleton.marketplace.prefix import prefix_site_dirs

    site = Path(prefix_site_dirs(str(prefix))[0])
    site.mkdir(parents=True, exist_ok=True)
    return site


def _write_dist_info(site: Path, dist_name: str, version: str, files: list[Path]) -> None:
    """A pip-shaped ``.dist-info`` for a distribution laid out under ``site``:
    ``METADATA`` naming the distribution and a ``RECORD`` listing its files (the
    listing ``importlib.metadata`` resolves ``Distribution.files`` from)."""
    dist_info = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n")
    record_rows = [f"{file.relative_to(site)},," for file in files]
    record_rows.append(f"{dist_info.relative_to(site)}/METADATA,,")
    record_rows.append(f"{dist_info.relative_to(site)}/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(record_rows) + "\n")


def _install_widget(
    site: Path,
    *,
    spec_yaml: str = _WIDGET_SPEC_YAML,
    spec_subpath: tuple[str, ...] = (),
    init_body: str = "",
) -> Path:
    """Lay a ``tai42-widget`` distribution into ``site`` the way ``pip install`` leaves one.

    The package tree, its spec (at ``tai42_widget/<spec_subpath>/tai-plugin.yml``), one
    chain file, and a dist-info whose RECORD lists them. Returns the package directory.
    """
    site.mkdir(parents=True, exist_ok=True)
    pkg = site / "tai42_widget"
    (pkg / "migrations").mkdir(parents=True)
    spec_dir = pkg.joinpath(*spec_subpath)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(init_body)
    (spec_dir / "tai-plugin.yml").write_text(spec_yaml)
    (pkg / "migrations" / "0001_baseline.sql").write_text("CREATE TABLE widget_items (id integer PRIMARY KEY);\n")
    _write_dist_info(
        site,
        "tai42-widget",
        "1.0.0",
        [pkg / "__init__.py", spec_dir / "tai-plugin.yml", pkg / "migrations" / "0001_baseline.sql"],
    )
    return pkg


def _configure_prefix(monkeypatch: pytest.MonkeyPatch, prefix: Path) -> None:
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: str(prefix))


def test_all_entries_two_skeleton_chains_without_installed_plugins(environment: Callable[..., None]) -> None:
    from tai42_skeleton.states.db import STATES_COMPONENT

    environment()

    # The two skeleton-owned chains, in order: the skeleton baseline first, then the
    # states record store.
    assert [entry.component for entry in all_migration_entries()] == [SKELETON_COMPONENT, STATES_COMPONENT]


def test_environment_plugin_resolves_against_its_install_directory(
    environment: Callable[..., None], tmp_path: Path
) -> None:
    pkg = _install_widget(tmp_path / "site")
    environment(tmp_path / "site")

    entries = installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-widget"]
    assert Path(str(entries[0].migrations_dir)) == pkg / "migrations"


def test_installed_plugin_entries_empty_when_database_not_configured(
    monkeypatch: pytest.MonkeyPatch, environment: Callable[..., None], tmp_path: Path
) -> None:
    # An unconfigured default database owns no store, so there are no plugin chains.
    _install_widget(tmp_path / "site")
    environment(tmp_path / "site")
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    assert installed_plugin_entries() == []


def test_prefix_preinstalled_plugin_discovered(
    monkeypatch: pytest.MonkeyPatch, environment: Callable[..., None], tmp_path: Path
) -> None:
    # A plugin pip-installed into the prefix is discovered although the prefix is not on
    # ``sys.path`` (a CLI process never activates it), resolved against the prefix filesystem.
    environment()
    pkg = _install_widget(_prefix_site(tmp_path / "prefix"))
    _configure_prefix(monkeypatch, tmp_path / "prefix")

    entries = installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-widget"]
    assert Path(str(entries[0].migrations_dir)) == pkg / "migrations"
    assert entries[0].migrations_dir.is_dir()


def test_distribution_in_environment_and_prefix_is_one_entry_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, environment: Callable[..., None], tmp_path: Path
) -> None:
    # The environment shadows the prefix for a package present in both, so the copy the
    # deployment imports is the one whose chain is discovered, once.
    env_pkg = _install_widget(tmp_path / "site")
    _install_widget(_prefix_site(tmp_path / "prefix"))
    environment(tmp_path / "site")
    _configure_prefix(monkeypatch, tmp_path / "prefix")

    entries = installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-widget"]
    assert Path(str(entries[0].migrations_dir)) == env_pkg / "migrations"


def test_dependency_without_spec_contributes_nothing(environment: Callable[..., None], tmp_path: Path) -> None:
    site = tmp_path / "site"
    pkg = site / "some_helper"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    _write_dist_info(site, "some-helper", "2.0.0", [pkg / "__init__.py"])
    environment(site)

    assert installed_plugin_entries() == []


def test_plugin_without_chain_contributes_nothing(environment: Callable[..., None], tmp_path: Path) -> None:
    _install_widget(tmp_path / "site", spec_yaml=_WIDGET_SPEC_YAML.replace("migrations: migrations\n", ""))
    environment(tmp_path / "site")

    assert installed_plugin_entries() == []


def test_spec_file_below_the_top_level_package_is_not_the_plugin_spec(
    environment: Callable[..., None], tmp_path: Path
) -> None:
    # The plugin spec is packaged beside the top-level package; a ``tai-plugin.yml`` deeper in
    # the tree (a template the distribution ships) is not one.
    _install_widget(tmp_path / "site", spec_yaml="not: a spec\n", spec_subpath=("templates", "plugin"))
    environment(tmp_path / "site")

    assert installed_plugin_entries() == []


def test_distribution_packaging_several_specs_is_loud(environment: Callable[..., None], tmp_path: Path) -> None:
    site = tmp_path / "site"
    files: list[Path] = []
    for top_level in ("tai42_widget_a", "tai42_widget_b"):
        (site / top_level).mkdir(parents=True)
        (site / top_level / "tai-plugin.yml").write_text(_WIDGET_SPEC_YAML)
        files.append(site / top_level / "tai-plugin.yml")
    _write_dist_info(site, "tai42-widget", "1.0.0", files)
    environment(site)

    with pytest.raises(MigrationDiscoveryError, match=r"several tai-plugin\.yml"):
        installed_plugin_entries()


def test_malformed_spec_is_a_loud_discovery_error(environment: Callable[..., None], tmp_path: Path) -> None:
    # A schema-invalid packaged spec fails discovery as a clean MigrationDiscoveryError
    # naming the distribution — never a raw traceback, never a silent skip.
    _install_widget(tmp_path / "site", spec_yaml="spec_version: 1\nname: widget\n")
    environment(tmp_path / "site")

    with pytest.raises(MigrationDiscoveryError, match="tai42-widget"):
        installed_plugin_entries()


def test_prefix_unset_or_never_created_adds_nothing(
    monkeypatch: pytest.MonkeyPatch, environment: Callable[..., None], tmp_path: Path
) -> None:
    environment()
    assert installed_plugin_entries() == []
    # A configured prefix whose site dirs do not exist (never installed into).
    _configure_prefix(monkeypatch, tmp_path / "never-created")
    assert installed_plugin_entries() == []


def test_installed_spec_is_read_without_importing_the_package(environment: Callable[..., None], tmp_path: Path) -> None:
    # Importing a plugin's package runs its module-level code (which may touch the unbound
    # app), so discovery reads the packaged spec off installed metadata and never imports it.
    _install_widget(tmp_path / "site", init_body="raise RuntimeError('discovery must never import a plugin package')\n")
    environment(tmp_path / "site")

    assert [entry.component for entry in installed_plugin_entries()] == ["tai42-widget"]
    assert "tai42_widget" not in sys.modules


# --- the modules a manifest loads -------------------------------------------


def _map_module_to_widget_spec(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Resolve the packaged spec of ``module``'s top-level package to the widget spec; any other ships none."""
    from tai42_skeleton.db import discovery

    top_level = module.partition(".")[0]
    monkeypatch.setattr(
        discovery,
        "_top_level_spec",
        lambda name, installed: (
            (_spec(package="tai42-widget", migrations="migrations"), Path("/site/tai42_widget"))
            if name == top_level
            else None
        ),
    )


def test_manifest_plugin_specs_cover_every_role_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plugin is found whichever role list the manifest loads it under — the set mirrors
    # the component importer's, not just one field.
    from tai42_skeleton.db.discovery import manifest_plugin_specs
    from tai42_skeleton.manifest import Manifest

    for field, value, module in (
        ("lifecycle_modules", ["tai42_widget.lifecycle"], "tai42_widget.lifecycle"),
        ("webhook_verifier_modules", ["tai42_widget.verify"], "tai42_widget.verify"),
        ("channel_modules", ["tai42_widget.channel"], "tai42_widget.channel"),
        ("routers_modules", ["tai42_widget.routes"], "tai42_widget.routes"),
        ("middlewares_modules", ["tai42_widget.mw"], "tai42_widget.mw"),
        ("extensions_modules", ["tai42_widget.ext"], "tai42_widget.ext"),
        ("backend_module", "tai42_widget.backend", "tai42_widget.backend"),
        ("tools", [{"module": "tai42_widget.tools", "title": "Widget"}], "tai42_widget.tools"),
        ("agents", [{"module": "tai42_widget.agent", "title": "Widget"}], "tai42_widget.agent"),
    ):
        _map_module_to_widget_spec(monkeypatch, module)
        specs = manifest_plugin_specs(
            Manifest.model_validate({field: value}), InstalledPackages(top_levels={}, editable_sources={})
        )
        assert list(specs) == ["tai42-widget"], field


def test_manifest_plugin_specs_skip_skeleton_routers() -> None:
    # The effective router set is composed of skeleton-owned modules that ship no plugin spec.
    from tai42_skeleton.db.discovery import manifest_plugin_specs
    from tai42_skeleton.manifest import Manifest

    manifest = Manifest.model_validate({"default_routers": "all"})

    assert manifest_plugin_specs(manifest, distribution_map()) == {}


# --- spec resolution for one top-level package -------------------------------


def test_top_level_spec_unmapped_top_level_yields_none() -> None:
    # A manifest module whose top-level import name maps to no installed distribution
    # contributes nothing — the same quiet no-spec outcome as an operator-authored module.
    from tai42_skeleton.db.discovery import _top_level_spec

    assert _top_level_spec("tai42_no_such_top_level_xyz", distribution_map()) is None


def test_top_level_spec_multi_dist_picks_the_one_shipping_the_spec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A top-level import name mapping to several distributions (a namespace package) resolves
    # the distribution that actually ships <top_level>/tai-plugin.yml; the others are skipped.
    from tai42_skeleton.db.discovery import _top_level_spec

    site = tmp_path / "site"
    namespace = site / "tai42_ns"
    namespace.mkdir(parents=True)
    (namespace / "helper.py").write_text("")
    (namespace / "tai-plugin.yml").write_text(_WIDGET_SPEC_YAML)
    _write_dist_info(site, "ns-helper", "1.0.0", [namespace / "helper.py"])
    _write_dist_info(site, "ns-plugin", "1.0.0", [namespace / "tai-plugin.yml"])
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()

    located = _top_level_spec(
        "tai42_ns", InstalledPackages(top_levels={"tai42_ns": ["ns-helper", "ns-plugin"]}, editable_sources={})
    )

    assert located is not None
    spec, package_root = located
    assert spec.package == "tai42-widget"
    assert package_root == namespace


def test_top_level_spec_malformed_packaged_spec_is_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A distribution that DOES ship a tai-plugin.yml but a schema-invalid one is a loud
    # MigrationDiscoveryError naming the distribution — never a silent skip, never a traceback.
    from tai42_skeleton.db.discovery import _top_level_spec

    _install_widget(tmp_path / "site", spec_yaml="spec_version: 1\nname: widget\n")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    importlib.invalidate_caches()

    with pytest.raises(MigrationDiscoveryError, match="tai42-widget"):
        _top_level_spec("tai42_widget", distribution_map())
