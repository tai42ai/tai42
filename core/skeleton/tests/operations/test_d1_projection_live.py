"""D.1 LIVE-stack verification of the MCP projection.

Boots the app through the real ``app.app_context`` harness with an ``api_tools``
manifest that loads no management tool modules and enables projection, then
asserts the projected surface end-to-end (checklist items 1-6):

1. the projected tool surface is exactly the expected op surface — the 134
   default-projected ops (178 total - 41 tier-2 default-excluded - 4 tier-1
   hardcode-blocked);
2. ``destructiveHint`` is present on destructive ops (a DELETE, a mutating POST)
   and absent on reads (a GET);
3. a manifest that BOTH hand-binds a tool named after a projected op AND
   projects that op fails boot loudly (the duplicate-bind collision guard);
4. a tier-2 op (``/api/auth/*`` / ``update_manifest``) is absent by default but
   projects when named in ``api_tools.include``;
5. a tier-1 meta-executor (``run_tool`` + ``submit_run`` + ``create_schedule``) stays
   hardcode-absent even when explicitly listed in ``api_tools.include`` (loud startup
   log);
6. ``user_tools`` curation still works alongside ``api_tools``.

These boot the FULL product router set so every operation carries its route
template + method (the tier-2 ``/api/auth/*`` prefix classification and the
DELETE-forces-destructive rule both need the route attached), matching a
realistic production manifest shape.
"""

from __future__ import annotations

import asyncio
import logging
import pkgutil

import pytest
from tai42_contract.manifest import ApiToolsConfig

import tai42_skeleton.routers as _routers_pkg
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.route_registry import load_all_routes, route_registry
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.projection import is_tier1, is_tier2, project_operations
from tai42_skeleton.operations.registry import operation_registry

# Infra router modules that carry NO projectable operation (metrics/health/native
# helpers). Excluded from the boot list so the stack loads only the operation-bearing
# routers — importing the prometheus/metrics modules mutates process-global
# multiproc state, which would leak into the metrics-CLI tests.
_INFRA_ROUTERS = frozenset(
    {"_tool_call", "health", "metrics", "metrics_settings", "observability_support", "prometheus", "tool_runs_settings"}
)


def _all_router_modules() -> list[str]:
    """Every OPERATION-bearing module in the product ``routers`` package — the HTTP
    surface a realistic deployment mounts, so each op's route template + method
    attach before projection (the tier-2 ``/api/auth/*`` prefix classification and
    the DELETE-forces-destructive rule both need the route attached)."""
    return [
        info.name
        for info in pkgutil.iter_modules(_routers_pkg.__path__, _routers_pkg.__name__ + ".")
        if info.name.rsplit(".", 1)[-1] not in _INFRA_ROUTERS
    ]


@pytest.fixture(scope="module", autouse=True)
def _restore_global_app_surface():
    """Snapshot and restore the global router/manifest state around this module so the
    full suite stays order-independent."""
    manifest_before = app._manifest
    app._manifest = None
    surface_before = {(m.path, m.methods) for m in load_all_routes()}  # offline full import
    routes_before = dict(route_registry._routes)
    ops_before = dict(operation_registry._operations)
    routing_before = {n: (m.route_template, m.http_method, m.path_params) for n, m in ops_before.items()}
    try:
        yield
    finally:
        app._manifest = manifest_before
        route_registry._routes.clear()
        route_registry._routes.update(routes_before)
        operation_registry._operations.clear()
        operation_registry._operations.update(ops_before)
        for name, (template, method, path_params) in routing_before.items():
            metadata = operation_registry._operations[name]
            metadata.route_template, metadata.http_method, metadata.path_params = template, method, path_params
        # The restored table must read back the complete offline surface — a partial
        # restore would leave the started/curated boot state leaking to the next reader.
        assert {(m.path, m.methods) for m in load_all_routes()} == surface_before


def _manifest(**api_tools: object) -> Manifest:
    body: dict = {"enabled": True}
    body.update(api_tools)
    # "none" pins the surface to exactly the operation-bearing routers listed here
    # (the infra routers are deliberately excluded), so the default core set does
    # not re-introduce the prometheus/metrics multiproc-state imports.
    return Manifest.model_validate(
        {"api_tools": body, "routers_modules": _all_router_modules(), "default_routers": "none"}
    )


class _RecordingTools:
    """Captures ``app.tools.tool(...)`` calls the projection makes, so a test can
    measure the projected set without binding onto the shared FastMCP server."""

    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *, force, name, tags, annotations):
        def decorator(func):
            self.registered[name] = annotations
            return func

        return decorator


class _RecordingApp:
    def __init__(self) -> None:
        self.tools = _RecordingTools()


# -- checklist 1: the projected surface is exactly the 133 default ops ---------


def test_d1_projected_surface_is_the_expected_op_count():
    async def run():
        async with app.app_context(_manifest()):
            reg = operation_registry
            ops = reg.all()
            total = len(ops)
            tier1 = sorted(op.name for op in ops if is_tier1(op))
            tier2 = sorted(op.name for op in ops if is_tier2(op) and not is_tier1(op))

            # The arithmetic: 178 total - 41 tier-2 default-excluded - 4 tier-1 hardcode-blocked = 133.
            # The +8 over the historical 143 are the settings-profile CRUD ops (list/get/put/
            # delete/diff/versions/version/rollback under /api/config/profiles*) — tier-0
            # default-projected like the sibling config ops (read_env/write_env/reload_config);
            # the HTTP secret/fenced action-class fences the door, not the MCP projection. The
            # +1 to 152 is ``apply_profile`` — authority_changing (tier-2, off the default MCP
            # surface): it replaces the whole env + recycles the fleet, never a default tool.
            # The +2 to 154 are the manifest ops ``get_manifest_preserved`` (the preserved
            # markers-intact read) and ``set_mcp_secret_env`` (the combined env+manifest secret
            # write) — both tier-0 default-projected like their siblings ``get_manifest`` /
            # ``set_mcp_config`` (the HTTP read/fenced action-class fences the door, not the
            # projection; neither is authority_changing). The +2 to 156 are the conversations
            # read doors ``list_conversation_threads`` / ``get_conversation_thread`` — tier-0
            # reads scoped by the route's own read-door policy. The +4 to 160 are the
            # conversation target-config CRUD ops ``list``/``get``/``set``/``delete_conversation_config``
            # — tier-0 default-projected (``set`` is destructive but not authority_changing, so
            # it stays on the default surface, like ``upsert_tool_meta``). The +1 to 161 is
            # ``get_mcp_env_refs`` — the manifest MCP section's !ENV-marker checklist (names +
            # set/unset booleans only), a tier-0 read like its ``get_manifest`` siblings. The +1
            # to 162 is ``delete_conversation_thread`` (the thread-forget door) — tier-0
            # default-projected like its ``delete_conversation_config`` sibling (destructive but
            # not authority_changing). The +7 to 169 are the granular manifest-family
            # add/remove ops ``add_mcp_entries``/``remove_mcp_entry``,
            # ``add_tools_entries``/``remove_tools_entry``, ``add_agents_entries``/``remove_agents_entry``
            # and ``modify_tool_extension_combos`` — all tier-0 default-projected (module-selection
            # writes; the combo door mirrors its ``set_tool_extensions`` sibling, neither
            # authority_changing). The +3 to 172 are ``update_api_tools`` and ``modify_role_grants``
            # (authority_changing) and ``modify_api_key_scopes`` (tier-2 by the ``/api/auth/`` route
            # prefix) — all off the default MCP surface. The +3 to 175 are the operator send + per-thread
            # mode doors ``send_conversation_thread_message`` / ``get_conversation_thread_mode`` /
            # ``set_conversation_thread_mode`` — tier-0 default-projected like their
            # ``delete_conversation_thread`` sibling (the send/set writes are destructive but not
            # authority_changing; the mode read is a plain GET). The +1 to 176 is
            # ``marketplace_install_preview`` — the no-side-effect install/update route preview,
            # a tier-0 read like the other marketplace reads (the HTTP fenced action-class fences
            # the door, not the projection; it is not authority_changing). The +2 to 178 are the
            # conversations filter/search + person-forget doors ``search_conversation_messages``
            # (a tier-0 read like ``list_conversation_threads``) and ``delete_conversation_person``
            # — tier-0 default-projected like its ``delete_conversation_thread`` sibling
            # (destructive but not authority_changing).
            # The +1 to 179 is ``sandbox_info`` — the sandbox identity + resolved-policy
            # read, tier-0 default-projected like its ``backend_info`` sibling (a plain read,
            # not authority_changing).
            assert total == 179, total
            # Tier-1 (never projectable): the three meta-executors, each running a
            # caller-named tool, plus ``get_me`` (``caller_context=True``).
            assert tier1 == ["create_schedule", "get_me", "run_tool", "submit_run"], tier1
            # The tier-2 set is the api_keys ops (all under /api/auth/*, including
            # create_claim_link) + the five role-management ops (create/edit/delete/
            # versions/rollback under /api/auth/roles*) + logout + exchange_claim_token
            # (authority_changing — a public credential door that must never project) +
            # import_backup + update_manifest + the four marketplace mutators + the two
            # plus the trigger-link mutators, register_hook, the two topic-verifier ops
            # (binding a lock REPLACES it, reaching the same state as unbinding),
            # create_conversation_route and apply_profile (the C5 env-replace + fleet-recycle
            # door), plus the granular-config authority ops update_api_tools and
            # modify_role_grants (authority_changing) and modify_api_key_scopes (tier-2 by the
            # ``/api/auth/`` route prefix, like the sibling api-key ops) — 41 in all. ``get_me``
            # is tier-1 hardcode-blocked, not tier-2.
            assert set(tier2) == {
                "add_scope_url",
                "apply_profile",
                "create_api_key",
                "create_claim_link",
                "create_conversation_route",
                "create_role",
                "create_trigger_link",
                "delete_role",
                "delete_scope",
                "delete_topic_verifier",
                "delete_trigger_link",
                "edit_api_key",
                "exchange_claim_token",
                "get_capabilities",
                "import_backup",
                "list_policy_versions",
                "list_public_routes",
                "list_role_versions",
                "list_roles",
                "list_routes",
                "list_scopes",
                "list_tokens_payload",
                "logout",
                "marketplace_install",
                "marketplace_uninstall",
                "marketplace_update",
                "marketplace_upgrade_all",
                "modify_api_key_scopes",
                "modify_role_grants",
                "pin_public_route",
                "register_hook",
                "remove_scope_url",
                "revoke_api_key",
                "set_topic_verifier",
                "rollback_policy",
                "rollback_role",
                "unpin_public_route",
                "update_api_tools",
                "update_manifest",
                "update_role",
                "validate_condition",
            }, tier2

            # The default-projected surface = 134 (the +1 is ``sandbox_info``), measured
            # two ways.
            recorder = _RecordingApp()
            projected = project_operations(recorder, ApiToolsConfig(), registry=reg)
            assert len(projected) == 134, len(projected)
            assert total - len(tier2) - len(tier1) == 134

            # The LIVE booted tool surface is the 134 projected ops PLUS the one
            # force-registered hidden mechanism tool the conversations router installs
            # at startup: ``conversation_deliver``, the completion continuation a resumed
            # async turn's driver fires through ``run_tool``. It is NOT an api_tools
            # projection (``projected`` stays 134) — it is a mandatory bridge registered
            # independently of the api_tools toggle and carried on the live surface like
            # any hidden tool, so the live count is 135.
            live = await app.tools.get_tools()
            assert set(live) == set(projected) | {"conversation_deliver"}
            assert (live["conversation_deliver"].meta or {}).get("tai42/hidden") is True
            assert len(live) == 135

            # Tier-1 and default tier-2 never appear on the live surface.
            assert "run_tool" not in live
            assert "submit_run" not in live
            assert "create_schedule" not in live
            assert "update_manifest" not in live  # tier-2, not included
            assert "delete_scope" not in live  # tier-2 by /api/auth prefix

    asyncio.run(run())


# -- checklist 2: destructiveHint on destructive ops, absent on reads ----------


def test_d1_destructive_hint_present_on_mutations_absent_on_reads():
    async def run():
        async with app.app_context(_manifest()):
            live = await app.tools.get_tools()

            def hint(name: str) -> object:
                assert name in live, f"{name} not projected"
                ann = live[name].annotations
                return getattr(ann, "destructiveHint", None) if ann is not None else None

            # A DELETE op (destructive auto-forced by the adapter).
            assert hint("unregister_hook") is True
            # A mutating POST op tagged destructive.
            assert hint("remove_tool") is True
            assert hint("notify_user") is True
            # A GET read carries no destructive hint.
            assert hint("list_system_kinds") in (None, False)
            assert hint("list_hooks") in (None, False)
            assert hint("list_channels") in (None, False)

    asyncio.run(run())


# -- checklist 3: the duplicate-bind collision guard fires at boot -------------


def test_d1_duplicate_bind_of_builtin_and_projection_fails_boot():
    """A manifest that hand-binds a tool named ``reload_config`` (via a
    ``tools[]`` module) while ``api_tools`` projects the SAME op name must fail
    boot loudly — never a running window with both surfaces. The tool binding
    raises on the duplicate name."""

    async def run():
        manifest = Manifest.model_validate(
            {
                "api_tools": {"enabled": True},
                "routers_modules": _all_router_modules(),
                "default_routers": "none",
                "tools": [{"title": "collide", "module": "tests.operations._fixtures.collide_projected"}],
            }
        )
        async with app.app_context(manifest):
            pass

    with pytest.raises(Exception, match="already exists"):
        asyncio.run(run())


# -- checklist 4: tier-2 absent by default, includable ------------------------


def test_d1_tier2_absent_by_default_but_projects_when_included():
    async def run():
        # Default: the tier-2 ops are off the live surface.
        async with app.app_context(_manifest()):
            live = await app.tools.get_tools()
            assert "update_manifest" not in live  # authority_changing flag
            assert "add_scope_url" not in live  # /api/auth/* prefix

        # Explicitly included: they project as real live tools.
        async with app.app_context(_manifest(include=["update_manifest", "add_scope_url"])):
            live = await app.tools.get_tools()
            assert "update_manifest" in live
            assert "add_scope_url" in live

    asyncio.run(run())


# -- checklist 5: tier-1 hardcode-blocked even when included -------------------


def test_d1_tier1_blocked_even_when_explicitly_included(caplog):
    async def run():
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations.projection"):
            async with app.app_context(_manifest(include=["run_tool", "submit_run", "create_schedule"])):
                live = await app.tools.get_tools()
                # No meta-executor is projected despite the explicit include.
                assert "run_tool" not in live
                assert "submit_run" not in live
                assert "create_schedule" not in live

    asyncio.run(run())

    # The block is loud: a WARNING names each meta-executor kept off the surface.
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("run_tool" in m and "hardcode-blocked meta-executor" in m for m in warned)
    assert any("submit_run" in m and "hardcode-blocked meta-executor" in m for m in warned)
    assert any("create_schedule" in m and "hardcode-blocked meta-executor" in m for m in warned)


# -- checklist 5b: get_me (caller-context identity op) is tier-1, never projectable --


def test_d1_get_me_is_caller_context_tier1_never_projectable(caplog):
    """``get_me`` returns the caller's OWN capability projection from identity params
    the HTTP edge injects; as an MCP tool a caller would supply those params itself and
    read ANY principal's projection. It is ``caller_context=True`` (tier-1), so it must
    never reach the tool surface — not by default, and not even when explicitly
    included — with a loud, accurately-reasoned block log."""

    async def run():
        async with app.app_context(_manifest()):
            reg = operation_registry
            get_me_op = reg.get("get_me")
            # Classified tier-1 (never projectable), NOT tier-2 (includable).
            assert is_tier1(get_me_op)
            assert not (is_tier2(get_me_op) and not is_tier1(get_me_op))

            # Absent from the default live surface AND from the raw projected set.
            live = await app.tools.get_tools()
            assert "get_me" not in live
            projected = project_operations(_RecordingApp(), ApiToolsConfig(), registry=reg)
            assert "get_me" not in projected

        # Even when explicitly included it stays off the surface, with a loud log naming
        # it a caller-context identity op (not a meta-executor — the reason is accurate).
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations.projection"):
            async with app.app_context(_manifest(include=["get_me"])):
                live = await app.tools.get_tools()
                assert "get_me" not in live

    asyncio.run(run())

    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("get_me" in m and "caller-context identity op" in m for m in warned)


# -- checklist 6: user_tools curation coexists with api_tools -----------------


def test_d1_user_tools_curation_coexists_with_api_tools():
    """``api_tools`` decides what is REGISTERED (projection); ``user_tools`` is the
    read-time flow-builder view filter, carried in the live manifest and exposed to
    the flow-builder surface. Both apply: the projected op is registered AND the
    curated ``user_tools`` subset is preserved."""

    async def run():
        manifest = Manifest.model_validate(
            {
                "api_tools": {"enabled": True},
                "routers_modules": _all_router_modules(),
                "default_routers": "none",
                "user_tools": ["remove_tool", "list_hooks"],
            }
        )
        async with app.app_context(manifest):
            # api_tools projected the surface: the 134 projected ops plus the
            # force-registered hidden ``conversation_deliver`` completion mechanism the
            # conversations router installs at startup (135 live).
            live = await app.tools.get_tools()
            assert "remove_tool" in live
            assert "list_hooks" in live
            assert len(live) == 135

            # user_tools curation is preserved and surfaced to the flow builder (the
            # read-time view over the registered set). It lives on the LIVE in-process
            # manifest — asserted via ``live_manifest_typed`` here, NOT via ``get_manifest``:
            # ``GET /api/manifest`` serves the PERSISTED store view (markers intact, no secret
            # leak), which this in-process harness does not persist, so ``get_manifest`` would
            # return the empty persisted view. The curation the checklist verifies is the live one.
            assert sorted(app.admin.live_manifest_typed.user_tools) == ["list_hooks", "remove_tool"]

    asyncio.run(run())
