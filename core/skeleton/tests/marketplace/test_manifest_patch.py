"""The pure manifest-patch functions, one behaviour per test, across every kind.

``collisions``/``apply_provides``/``remove_provides`` are side-effect-free over a
plain manifest dict, so each patch shape (config row, module list, package list,
scalar slot, env-selected no-op) is checked in isolation, plus the convergence and
collision semantics and the final ``Manifest.model_validate`` acceptance.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.route_defaults import STUDIO_SPA_ROUTER
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace import manifest_patch
from tai42_skeleton.marketplace.errors import ManifestBindingError, ManifestCollisionError
from tai42_skeleton.marketplace.manifest_patch import apply_provides, collisions, remove_provides

from ._specs import connector_item, make_spec


def _item(kind: str, name: str, module: str) -> dict:
    item: dict = {"kind": kind, "name": name, "module": module, "description": "d"}
    if kind == "router":
        # A router item now REQUIRES a routes block; manifest patching is orthogonal
        # to route content, so a minimal generic declaration keeps these tests focused.
        item["routes"] = {"base": name, "paths": [{"path": "/ping", "methods": ["GET"], "public": False}]}
    return item


# -- apply per kind ----------------------------------------------------------


def test_apply_tool_adds_a_config_row_titled_by_module() -> None:
    spec = make_spec(provides=[_item("tool", "gen-uuid", "pkg.tools.uuid")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["tools"] == [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]


def test_apply_agent_adds_a_config_row() -> None:
    spec = make_spec(provides=[_item("agent", "helper", "pkg.agents.helper")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["agents"] == [{"title": "pkg.agents.helper", "module": "pkg.agents.helper"}]


def test_apply_tools_sharing_a_module_coalesce_to_one_entry() -> None:
    spec = make_spec(
        provides=[
            _item("tool", "gen-uuid", "pkg.tools.multi"),
            _item("tool", "gen-ulid", "pkg.tools.multi"),
        ]
    )
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["tools"] == [{"title": "pkg.tools.multi", "module": "pkg.tools.multi"}]


def test_apply_extension_appends_to_module_list() -> None:
    spec = make_spec(provides=[_item("extension", "ext", "pkg.ext")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["extensions_modules"] == ["pkg.ext"]


def test_apply_channel_appends_to_channel_modules() -> None:
    spec = make_spec(provides=[_item("channel", "chan", "pkg.chan")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["channel_modules"] == ["pkg.chan"]


def test_apply_identity_targets_lifecycle_modules() -> None:
    spec = make_spec(provides=[_item("identity", "idp", "pkg.idp")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["lifecycle_modules"] == ["pkg.idp"]


def test_apply_connector_appends_provider_descriptor_to_connectors() -> None:
    # A connector is a DATA item: its ``provider`` descriptor is appended to the
    # manifest ``connectors`` list (descriptor_entry), never a module.
    spec = make_spec(package=None, provides=[connector_item("acme")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert [entry["id"] for entry in manifest["connectors"]] == ["acme"]
    assert manifest["connectors"][0]["kind"] == "oauth"
    assert "module" not in manifest["connectors"][0]


def test_connector_collides_on_existing_provider_id() -> None:
    spec = make_spec(package=None, provides=[connector_item("acme")])
    manifest: dict = {"connectors": [{"id": "acme", "kind": "none"}]}
    found = collisions(manifest, spec)
    assert found == ["connectors entry with id 'acme' already exists"]
    with pytest.raises(ManifestCollisionError):
        apply_provides(manifest, spec)


def test_connector_remove_drops_by_id_and_is_convergent() -> None:
    spec = make_spec(package=None, provides=[connector_item("acme")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert remove_provides(manifest, spec) is True
    assert manifest["connectors"] == []
    # A second removal is a convergent no-op, never an error.
    assert remove_provides(manifest, spec) is False


def test_connector_and_tool_coexist_in_one_spec() -> None:
    # A community-style spec mixing a code tool with a data connector: the tool binds
    # to ``tools`` (config_row) and the connector to ``connectors`` (descriptor_entry).
    spec = make_spec(provides=[_item("tool", "gen-uuid", "pkg.tools.uuid"), connector_item("iota")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["tools"] == [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]
    assert [entry["id"] for entry in manifest["connectors"]] == ["iota"]


def test_apply_webhook_verifier_appends_to_its_module_list() -> None:
    spec = make_spec(provides=[_item("webhook-verifier", "wv", "pkg.wv")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["webhook_verifier_modules"] == ["pkg.wv"]


def test_apply_studio_plugin_appends_the_package_name_not_the_module() -> None:
    spec = make_spec(package="tai-studio-ext", provides=[_item("studio-plugin", "sp", "pkg.studio.entry")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    # package_list stores the DISTRIBUTION name, never the item's module.
    assert manifest["studio_plugins"] == ["tai-studio-ext"]


def test_apply_scalar_backend_sets_the_slot_to_the_package_root() -> None:
    # The descriptor names the impl submodule where the Backend class lives; the
    # slot must hold the top-level import package, whose __init__ registers the
    # provider and its sibling tool/extension modules that the skeleton whitelists
    # under that root. Naming the submodule leaves the siblings un-whitelisted and
    # aborts boot with CorePluginBootError.
    spec = make_spec(provides=[_item("backend", "rq", "tai42_backend_rq.backend")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["backend_module"] == "tai42_backend_rq"


@pytest.mark.parametrize(
    ("kind", "field", "module", "root"),
    [
        ("storage", "storage_module", "tai42_storage_s3.storage", "tai42_storage_s3"),
        ("monitoring", "monitoring_module", "tai42_monitoring_langfuse.register", "tai42_monitoring_langfuse"),
        ("sandbox", "sandbox_module", "tai42_sandbox_docker.provider", "tai42_sandbox_docker"),
    ],
)
def test_apply_scalar_slot_holds_the_package_root(kind: str, field: str, module: str, root: str) -> None:
    spec = make_spec(provides=[_item(kind, "x", module)])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest[field] == root


def test_apply_scalar_module_already_a_root_is_unchanged() -> None:
    # A descriptor whose module IS the package root (no impl submodule) is written
    # verbatim: the top-level derivation is idempotent.
    spec = make_spec(provides=[_item("storage", "local", "tai42_storage_local")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["storage_module"] == "tai42_storage_local"


# -- router/middleware ordering-aware module_list ----------------------------


def test_apply_router_inserts_before_the_spa_catch_all() -> None:
    # A plugin router must land BEFORE the SPA catch-all, else its routes are dead
    # (the catch-all matches every path).
    spec = make_spec(provides=[_item("router", "r", "pkg.routers.r")])
    manifest = {"routers_modules": ["tai42_skeleton.routers.agents", STUDIO_SPA_ROUTER]}
    apply_provides(manifest, spec)
    assert manifest["routers_modules"] == [
        "tai42_skeleton.routers.agents",
        "pkg.routers.r",
        STUDIO_SPA_ROUTER,
    ]


def test_apply_router_plain_appends_when_catch_all_absent() -> None:
    # No catch-all in the list (the loader owns its placement under all/api) → the
    # router plain-appends.
    spec = make_spec(provides=[_item("router", "r", "pkg.routers.r")])
    manifest = {"routers_modules": ["tai42_skeleton.routers.agents"]}
    apply_provides(manifest, spec)
    assert manifest["routers_modules"] == ["tai42_skeleton.routers.agents", "pkg.routers.r"]


def test_apply_router_into_empty_list_appends() -> None:
    spec = make_spec(provides=[_item("router", "r", "pkg.routers.r")])
    manifest: dict = {}
    apply_provides(manifest, spec)
    assert manifest["routers_modules"] == ["pkg.routers.r"]


def test_apply_two_routers_keep_relative_order_before_catch_all() -> None:
    spec = make_spec(
        provides=[
            _item("router", "r1", "pkg.routers.r1"),
            _item("router", "r2", "pkg.routers.r2"),
        ]
    )
    manifest = {"routers_modules": [STUDIO_SPA_ROUTER]}
    apply_provides(manifest, spec)
    assert manifest["routers_modules"] == ["pkg.routers.r1", "pkg.routers.r2", STUDIO_SPA_ROUTER]


def test_remove_router_leaves_the_catch_all_last() -> None:
    spec = make_spec(provides=[_item("router", "r", "pkg.routers.r")])
    manifest = {"routers_modules": ["pkg.routers.r", STUDIO_SPA_ROUTER]}
    assert remove_provides(manifest, spec) is True
    assert manifest["routers_modules"] == [STUDIO_SPA_ROUTER]


def test_apply_middleware_plain_appends_to_middlewares_modules() -> None:
    # MIDDLEWARE has no catch-all; it plain-appends (no ordering rule).
    spec = make_spec(provides=[_item("middleware", "m", "pkg.mw.m")])
    manifest = {"middlewares_modules": ["pkg.mw.first"]}
    apply_provides(manifest, spec)
    assert manifest["middlewares_modules"] == ["pkg.mw.first", "pkg.mw.m"]


def test_router_collides_when_already_present() -> None:
    spec = make_spec(provides=[_item("router", "r", "pkg.routers.r")])
    manifest = {"routers_modules": ["pkg.routers.r", STUDIO_SPA_ROUTER]}
    assert collisions(manifest, spec) == ["routers_modules already contains 'pkg.routers.r'"]


# -- collisions per field shape ----------------------------------------------


def test_config_row_collides_on_existing_module() -> None:
    spec = make_spec(provides=[_item("tool", "gen-uuid", "pkg.tools.uuid")])
    manifest = {"tools": [{"title": "other", "module": "pkg.tools.uuid"}]}
    found = collisions(manifest, spec)
    assert found == ["tools entry with module 'pkg.tools.uuid' already exists"]


def test_config_row_collides_on_existing_title() -> None:
    spec = make_spec(provides=[_item("tool", "gen-uuid", "pkg.tools.uuid")])
    # An existing entry whose TITLE equals the incoming module is a collision too.
    manifest = {"tools": [{"title": "pkg.tools.uuid", "module": "something.else"}]}
    assert collisions(manifest, spec) == ["tools entry with module 'pkg.tools.uuid' already exists"]


def test_module_list_collides_on_exact_string() -> None:
    spec = make_spec(provides=[_item("extension", "ext", "pkg.ext")])
    manifest = {"extensions_modules": ["pkg.ext"]}
    assert collisions(manifest, spec) == ["extensions_modules already contains 'pkg.ext'"]


def test_scalar_collides_when_slot_is_truthy() -> None:
    spec = make_spec(provides=[_item("backend", "be", "pkg.backend")])
    manifest = {"backend_module": "existing_pkg"}
    found = collisions(manifest, spec)
    assert found == ["backend_module is already set to 'existing_pkg' (cannot install 'pkg')"]


def test_empty_scalar_slot_is_not_a_collision() -> None:
    # The example manifest uses "" for an unset scalar slot; "" is falsy, so no clash.
    spec = make_spec(provides=[_item("backend", "be", "pkg.backend")])
    assert collisions({"backend_module": ""}, spec) == []


def test_scalar_self_conflict_two_distinct_roots_one_slot_is_a_collision() -> None:
    # One spec providing two backend items whose top-level packages differ is a
    # self-conflict for the single backend slot: a last-write-wins apply would
    # silently drop the first root. (Two items sharing one root collapse to that
    # root — the package import registers both, so the slot names it once.)
    spec = make_spec(
        provides=[
            _item("backend", "be-a", "pkg_a.backend"),
            _item("backend", "be-b", "pkg_b.backend"),
        ]
    )
    found = collisions({}, spec)
    assert found == ["backend_module is a single-module slot but this plugin provides 'pkg_a', 'pkg_b'"]


def test_scalar_self_conflict_blocks_apply() -> None:
    spec = make_spec(
        provides=[
            _item("backend", "be-a", "pkg_a.backend"),
            _item("backend", "be-b", "pkg_b.backend"),
        ]
    )
    manifest: dict = {}
    with pytest.raises(ManifestCollisionError, match="single-module slot"):
        apply_provides(manifest, spec)
    # Nothing was written — no silent last-write-wins drop.
    assert "backend_module" not in manifest


def test_apply_raises_listing_every_collision() -> None:
    spec = make_spec(
        provides=[
            _item("tool", "gen-uuid", "pkg.tools.uuid"),
            _item("extension", "ext", "pkg.ext"),
        ]
    )
    manifest = {"tools": [{"title": "t", "module": "pkg.tools.uuid"}], "extensions_modules": ["pkg.ext"]}
    with pytest.raises(ManifestCollisionError) as exc:
        apply_provides(manifest, spec)
    assert "pkg.tools.uuid" in str(exc.value)
    assert "pkg.ext" in str(exc.value)


# -- remove semantics --------------------------------------------------------


def test_remove_drops_exactly_the_specs_references() -> None:
    spec = make_spec(provides=[_item("tool", "gen-uuid", "pkg.tools.uuid")])
    manifest = {"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}, {"title": "keep", "module": "keep"}]}
    changed = remove_provides(manifest, spec)
    assert changed is True
    assert manifest["tools"] == [{"title": "keep", "module": "keep"}]


def test_remove_is_convergent_second_run_returns_false() -> None:
    spec = make_spec(provides=[_item("extension", "ext", "pkg.ext")])
    manifest = {"extensions_modules": ["pkg.ext"]}
    assert remove_provides(manifest, spec) is True
    assert remove_provides(manifest, spec) is False


def test_remove_drops_a_tools_entry_by_module_even_when_title_was_edited() -> None:
    spec = make_spec(provides=[_item("tool", "gen-uuid", "pkg.tools.uuid")])
    # An operator renamed the title; removal still matches on module (a leftover
    # entry would brick the next boot after the pip uninstall).
    manifest = {"tools": [{"title": "operator-renamed", "module": "pkg.tools.uuid"}]}
    assert remove_provides(manifest, spec) is True
    assert manifest["tools"] == []


def test_remove_leaves_a_foreign_scalar_value_alone() -> None:
    spec = make_spec(provides=[_item("backend", "be", "pkg.backend")])
    # The operator replaced the slot with a different package; removal must not clear it.
    manifest = {"backend_module": "operator_replacement"}
    assert remove_provides(manifest, spec) is False
    assert manifest["backend_module"] == "operator_replacement"


def test_remove_clears_a_scalar_still_holding_the_specs_package_root() -> None:
    # Uninstall matches the package root that apply wrote, not the descriptor's
    # impl submodule.
    spec = make_spec(provides=[_item("backend", "be", "pkg.backend")])
    manifest = {"backend_module": "pkg"}
    assert remove_provides(manifest, spec) is True
    assert manifest["backend_module"] is None


# -- env-selected config: documented no-op across all three ------------------


def test_config_kind_is_a_no_op_everywhere() -> None:
    spec = make_spec(provides=[_item("config", "vault", "pkg.config.vault")])
    manifest: dict = {}
    assert collisions(manifest, spec) == []
    apply_provides(manifest, spec)
    assert manifest == {}  # nothing applied — pip install/uninstall is the registration
    assert remove_provides(manifest, spec) is False


# -- unknown kind ------------------------------------------------------------


def test_unknown_kind_raises_binding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A kind with no binding is contract drift; the mapping is patched to drop the
    # tool binding so the loud raise is exercised rather than silently skipped. The
    # typed error maps to a server-side 500 at the operation boundary, never a 400.
    spec = make_spec(provides=[_item("tool", "gen-uuid", "pkg.tools.uuid")])
    monkeypatch.setattr(manifest_patch, "KIND_MANIFEST_BINDINGS", {})
    with pytest.raises(ManifestBindingError, match="no manifest binding"):
        collisions({}, spec)


# -- the patched dict validates ----------------------------------------------


def test_patched_manifest_validates() -> None:
    spec = make_spec(
        provides=[
            _item("tool", "gen-uuid", "pkg.tools.uuid"),
            _item("extension", "ext", "pkg.ext"),
            _item("backend", "be", "pkg.backend"),
        ]
    )
    manifest: dict = {}
    apply_provides(manifest, spec)
    # A malformed compose would raise here; the patched dict must be a valid Manifest.
    Manifest.model_validate(manifest)


# -- composed: the installed scalar slot boots the skeleton loader -----------


@pytest.mark.parametrize(
    ("kind", "field", "impl_module", "sibling_module"),
    [
        ("backend", "backend_module", "tai42_backend_rq.backend", "tai42_backend_rq.tools"),
        ("monitoring", "monitoring_module", "tai42_monitoring_x.register", "tai42_monitoring_x.tools"),
        ("storage", "storage_module", "tai42_storage_x.storage", "tai42_storage_x.tools"),
        ("sandbox", "sandbox_module", "tai42_sandbox_x.provider", "tai42_sandbox_x.tools"),
    ],
)
def test_installed_scalar_slot_whitelists_the_plugins_sibling_modules(
    kind: str, field: str, impl_module: str, sibling_module: str
) -> None:
    # The composed consumer path: the marketplace write (apply_provides) feeds the
    # skeleton manifest loader. Importing a scalar plugin runs its package __init__,
    # which imports sibling tool/extension modules; each @app.tool call there consults
    # should_include_tool with the SIBLING module path. The loader whitelists every
    # module under the slot's package root (_is_plugin_module), so the sibling passes.
    # On the unfixed write the slot names the impl SUBMODULE, the sibling falls outside
    # the whitelist, and should_include_tool raises "not found in manifest" — the exact
    # ImportError the boot wraps as CorePluginBootError, aborting a marketplace-installed
    # backend/monitoring/storage/sandbox plugin.
    spec = make_spec(provides=[_item(kind, "x", impl_module)])
    manifest_dict: dict = {}
    apply_provides(manifest_dict, spec)
    assert manifest_dict[field] == impl_module.partition(".")[0]
    manifest = Manifest.model_validate(manifest_dict)
    assert manifest.should_include_tool("x_probe", sibling_module) is True
