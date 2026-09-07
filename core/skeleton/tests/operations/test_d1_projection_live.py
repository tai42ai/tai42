"""D.1 LIVE-stack verification of the MCP projection.

Boots the app through the real ``app.app_context`` harness with an ``api_tools``
manifest that loads no management tool modules and enables projection, then
asserts the projected surface end-to-end (checklist items 1-6):

1. the projected tool surface is exactly the expected op surface — the 165
   default-projected ops (210 total - 41 tier-2 default-excluded - 4 tier-1
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


# -- checklist 1: the projected surface is exactly the 165 default ops ---------


def test_d1_projected_surface_is_the_expected_op_count():
    async def run():
        async with app.app_context(_manifest()):
            reg = operation_registry
            ops = reg.all()
            total = len(ops)
            tier1 = sorted(op.name for op in ops if is_tier1(op))
            tier2 = sorted(op.name for op in ops if is_tier2(op) and not is_tier1(op))

            # The registered surface decomposes by projection tier (each asserted below):
            # 210 total = 4 tier-1 (hardcode-blocked from projection) + 41 tier-2
            # (default-excluded, includable) + 165 tier-0 (default-projected). A destructive
            # tier-0 op is still default-projected under ``expose_destructive`` — a data write
            # or purge (``delete_*``, ``erase_*``, ``prune_runs``, ``prune_state_retention``)
            # is destructive but not authority_changing, so it stays tier-0 like
            # ``delete_conversation_config``; authority_changing writes (roles, api keys,
            # api_tools) and the ``/api/auth/`` family are the tier-2 set. The 26 states-surface
            # doors (module + state ``list``/``get``/``put``/``delete``, the record CRUD plus
            # ``search``/``fold``/``apply`` doors, the mount/migrate/subjects/consumers/stats
            # reads, and ``prune_state_retention``) are all tier-0 CRUD over the states service.
            assert total == 210, total
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

            # The default-projected surface = 165 (every op that is neither tier-1 nor
            # tier-2, destructive tier-0 ops included under the default ``expose_destructive``),
            # measured two ways.
            recorder = _RecordingApp()
            projected = project_operations(recorder, ApiToolsConfig(), registry=reg)
            assert len(projected) == 165, len(projected)
            assert total - len(tier2) - len(tier1) == 165

            # The LIVE booted tool surface is the 165 projected ops PLUS the two
            # force-registered hidden mechanism tools the conversations router installs at
            # startup: ``conversation_deliver`` (a parked AGENT turn's resumed answer) and
            # ``deliver_tool_completion`` (a parked TOOL turn's terminal, mapped via reply_expr),
            # both fired through ``run_tool`` by the resumer. Neither is an api_tools projection
            # (``projected`` stays 165) — they are mandatory bridges registered independently of
            # the api_tools toggle and carried on the live surface like any hidden tool, so the
            # live count is 167.
            hidden_bridges = {"conversation_deliver", "deliver_tool_completion"}
            live = await app.tools.get_tools()
            assert set(live) == set(projected) | hidden_bridges
            for name in hidden_bridges:
                assert (live[name].meta or {}).get("tai42/hidden") is True
            assert len(live) == 167

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


# -- interactions read tools are agent-callable through the projected surface ---


def test_d1_interactions_read_tools_are_agent_callable():
    """``list_interactions`` and ``list_pending_interactions`` are agent-callable platform
    tools via the SAME operation-projection surface (and the SAME ``tai42_app.tools``
    registry + ``run_tool`` dispatch) that carries ``ask_user`` and ``answer_interaction``:
    both are default-projected reads, so an agent dispatches them by name. A same-named
    ``tools/builtin`` shim is impossible — it would trip the duplicate-bind boot guard
    (checklist 3) — so the projected surface IS the agent tool. This pins registration +
    invocation returning the operation's own result."""

    async def run():
        async with app.app_context(_manifest()):
            live = await app.tools.get_tools()
            assert "list_interactions" in live
            assert "list_pending_interactions" in live

            # Dispatched by name through the shared run_tool seam, returning the
            # operation's own result. The interactions store is unconfigured in this
            # harness, so each read answers its honest empty payload.
            page = await app.tools.run_tool("list_interactions", {})
            assert page == {
                "items": [],
                "total": 0,
                "page": 1,
                "page_size": 50,
                "next_page": None,
                "truncated": False,
            }
            parked = await app.tools.run_tool("list_pending_interactions", {})
            assert parked == {"items": [], "count": 0}

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
            # api_tools projected the surface: the 165 projected ops plus the two
            # force-registered hidden completion mechanisms the conversations router installs at
            # startup (``conversation_deliver`` + ``deliver_tool_completion``), so 167 live.
            live = await app.tools.get_tools()
            assert "remove_tool" in live
            assert "list_hooks" in live
            assert len(live) == 167

            # user_tools curation is preserved and surfaced to the flow builder (the
            # read-time view over the registered set). It lives on the LIVE in-process
            # manifest — asserted via ``live_manifest_typed`` here, NOT via ``get_manifest``:
            # ``GET /api/manifest`` serves the PERSISTED store view (markers intact, no secret
            # leak), which this in-process harness does not persist, so ``get_manifest`` would
            # return the empty persisted view. The curation the checklist verifies is the live one.
            assert sorted(app.admin.live_manifest_typed.user_tools) == ["list_hooks", "remove_tool"]

    asyncio.run(run())
