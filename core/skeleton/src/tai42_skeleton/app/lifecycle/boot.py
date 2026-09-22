"""Reset the process registries and drive the per-epoch rebuild (start/reload)."""

import logging
from typing import Any

from fastmcp.prompts import Prompt
from fastmcp.resources import Resource
from fastmcp.resources.template import ResourceTemplate
from fastmcp.tools import Tool
from tai42_contract.accounts import reset_registry as reset_accounts_registry
from tai42_contract.app import tai42_app

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.boot_rules import require_bus_for_backend
from tai42_skeleton.app.kind_status import warn_if_noop_monitoring
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.connectors.providers.registry import reset_registry
from tai42_skeleton.conversations.target_validators import register_platform_target_validators
from tai42_skeleton.extensions import ExtensionRegistry
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.middleware.rate_limit import warn_if_rate_limiting_off
from tai42_skeleton.operations.registry import operation_registry
from tai42_skeleton.tools import ToolRegistry

logger = logging.getLogger(__name__)


class BootMixin(LifecycleState):
    """Lifecycle mixin that boots the app: claims the global handle and brings up the manifest's components."""

    def start(self, manifest: Manifest):
        """Boot the app from ``manifest``: claim the global ``tai42_app`` handle and bring up its components."""
        # Booting is the composition root: this is where the running impl claims
        # the global ``tai42_app`` handle. Constructing a ``TaiMCP`` must not — only
        # start()/app_context binds, so building a throwaway app can't hijack it.
        tai42_app.bind(self)
        self._manifest = manifest
        self._failed_mcps = {}
        self._mcp_bound_tools = {}
        # Reset so a dropped agent doesn't linger across update()/reload —
        # importer.py re-fires the @tai42_app.agents.agent decorator each start().
        self._agent_binding.reset()

        # Reset so a dropped webhook verifier doesn't linger across update()/reload
        # — the manifest's verifier modules re-run their register() call each
        # start(). Mirrors the agent reset above.
        self._webhook_verifier_registry.reset()

        # Reset so a dropped channel doesn't linger across update()/reload — the
        # manifest's channel modules re-run their register() call each start().
        # Mirrors the webhook-verifier reset above.
        self._channel_registry.reset()

        # Reset so a dropped preset write validator doesn't linger across
        # update()/reload — the manifest's tool modules re-run their
        # register_write_validator() call each start(). Mirrors the resets above.
        self._write_validator_registry.reset()

        # Reset so a dropped conversation target validator doesn't linger across
        # update()/reload — the manifest's plugin modules re-run their
        # register_target_validator() call each start(). Mirrors the reset above.
        # Then re-register the platform's own validators (the ``agent`` kind), which the
        # skeleton owns rather than a plugin module, before any plugin registers its kinds.
        self._target_validator_registry.reset()
        register_platform_target_validators(self._target_validator_registry)

        # Reset the per-base-tool preset input-schema support + registration-tier
        # declarations alongside the write validator, for the same reload reason. The
        # tier registry backs both the authoring gate and the run-time fence.
        self._input_schema_support_registry.reset()
        self._registration_tier_registry.reset()

        # Reset so a dropped tool-references declaration doesn't linger across
        # update()/reload — the manifest's tool modules re-run their
        # @app.tools.tool(tool_refs=...) decorator each start(). Mirrors the reset above.
        self._tool_refs_registry.reset()

        # Reset the per-tool retry-policy declarations for the same reload reason —
        # a dropped @app.tools.tool(retry=...) declaration must not keep retrying a
        # tool that no longer claims idempotency.
        self._tool_retry_registry.reset()

        # Reset the per-tool door-extras declarations for the same reload reason — a
        # dropped @app.tools.tool(extras_keys=...) declaration must not keep admitting an
        # extras key a tool no longer reads.
        self._tool_extras_registry.reset()

        # Reset the rename-referee collection and the declared-preset-seed registry
        # alongside the registries above: a reload re-imports the plugin modules (which
        # re-run their register_rename_referee/register_seed calls) and the
        # platform-internal referees re-arm through their startup/reload handler, so a
        # stale referee or a dropped seed never lingers across update()/reload.
        self._rename_referee_registry.reset()
        self._delete_referee_registry.reset()
        self._detach_referee_registry.reset()
        self._seed_registry.reset()

        # Reset the state store's consumer-owned registries (attach validators, attach
        # reconcilers, consumer listers, template seeds) alongside the registries above: a
        # reload re-imports the plugin modules (which re-run their register_attach_validator/
        # register_attach_reconciler/register_consumer_lister/register_template_seed calls), so a
        # stale validator, reconciler, lister or seed never lingers.
        self._states_attach_validators.reset()
        self._states_attach_reconcilers.reset()
        self._states_consumer_listers.reset()
        self._states_template_seeds.reset()

        # Drop the cached resource manager: a reload re-imports the storage
        # module and rebuilds the storage provider, so a stale cache would keep
        # rendering against (and pin open) the previous provider's pool.
        self._resource_manager_cache = None

        # Clear the connector registry's write-target generation before
        # _initialize_components() re-registers the manifest's ``connectors`` entries
        # during boot/reload; the registry's duplicate guard would otherwise crash on
        # the re-registration. During an epoch build this clears the fresh STAGED
        # generation (the live catalog stays untouched); at boot the committed map.
        # Mirrors the _agents reset.
        reset_registry()

        # Clear the identity-provider registry's write-target generation before
        # _initialize_components() re-imports the manifest's identity-plugin modules,
        # which re-run their module-level register_identity_provider(...) calls.
        # The skeleton ships NO concrete identity provider: a deployment names one
        # (e.g. tai42_identity_redis.redis_api_key_provider, the default in the example
        # manifest) in its manifest lifecycle_modules, which _initialize_components
        # imports below — that import-only registration is the sole home, exactly as
        # the shared_secret webhook verifier registers.
        _lifecycle.reset_identity_registry()

        # Clear the accounts-provider registry beside the identity reset: an accounts
        # provider registers into BOTH registries on import, so without this mirror an
        # in-process reload would clear identity but not accounts, and the duplicate
        # guard in register_accounts_provider would crash the reload once an accounts
        # plugin is installed.
        reset_accounts_registry()

        # Drop sub-MCP routes + cached sub-apps so a re-init stops serving
        # sub-apps from the previous generation; the reload handlers below
        # re-register the current ones. Their lifespans tear down on the loop
        # that owns them.
        self._mcp_sub_app_router.reset()

        # Clear the live tool/prompt/resource/template surface before
        # _initialize_components() re-imports the manifest modules and re-fires their
        # module-level @tai42_app.tools.tool (and any module-registered prompt /
        # resource / resource-template) decorators, so each re-registration lands in
        # a clean surface and never trips on_duplicate="error". Mirrors the
        # agent/webhook/channel/identity resets above: tools are the same
        # shape (a module decorator re-fired every reload), so they get the same
        # reset-before-reimport treatment rather than relying on the CALLER's removal
        # being atomic with the reimport — an interleaving reload whose caller-side
        # removal had not fully cleared the surface would otherwise re-add a
        # still-present tool and crash the reload with "Component already exists".
        self._reset_component_surface()

        # Clear the operation registry's write-target generation before
        # _initialize_components() re-imports the router modules, which re-fire their
        # module-level @operation decorators; without this a reload would trip the
        # duplicate-name guard. During an epoch build this clears the fresh STAGED
        # generation and the replay/route-attach/projection below all read it, so the
        # live committed surface keeps answering authz/dispatch against a complete
        # generation the whole time — no unsettled window on the request path. At
        # boot it clears the committed map.
        operation_registry.clear()

        # Clear the route registry's write-target /api shape generation before the
        # routers re-attach: an epoch build clears the fresh STAGED generation (the
        # live match/collision surface stays untouched); at boot the committed one.
        # The re-import below repopulates it, so an uninstalled/remapped route leaves
        # the shape index by simply not re-registering.
        _lifecycle.route_registry.reset_shape_index()

        self._initialize_registries()
        self._initialize_components()

        # The route index must be dropped HERE, AFTER the reimport re-attached every
        # route: a request served in the reload window can have rebuilt it against the
        # previous surface. A stale index resolves an added route to None, which denies
        # every caller at the tool edge and skips the route's fence at the request gate.
        from tai42_skeleton.access_control.role_gate import reset_route_index

        reset_route_index()

        logger.info("[tools]")
        for t in sorted(self._registry_names_sync()["tool"]):
            logger.info(f"\t. {t}")

        # The pluggable-kind summary: one line per kind's live active/default/off
        # state (a broken registry raises here and fails the boot, never a silent
        # partial table), plus the once-per-process warning when NoOp monitoring is
        # the active recorder — the point where "not configured" becomes
        # distinguishable from "no traffic".
        kind_rows = _lifecycle.collect_kind_status()
        logger.info("[kinds]")
        for row in kind_rows:
            suffix = f" ({row.plugin})" if row.plugin else ""
            logger.info(f"\t. {row.kind}: {row.state}{suffix} — {row.detail}")
        warn_if_noop_monitoring(kind_rows, logger)
        # The public doors run unthrottled when no Redis backs the rate limiter —
        # one loud once-per-process WARNING here (never a boot refusal).
        warn_if_rate_limiting_off(logger)

    def _reload_registries(self, manifest: Manifest) -> dict[str, Any]:
        """Re-initialise the process registries from ``manifest`` and run the per-epoch handler list ONCE.

        The rebuild step the epoch build+swap primitive calls.
        The proposed env is already live in ``os.environ`` and the settings caches
        were cleared by the primitive, so this resolves every settings read under the
        env about to be persisted. It raises loudly on ANY failure so the
        primitive discards the half-built epoch and restores the env — there is no
        restore-on-failure dance here (the primitive owns the discard).
        """
        # Reload-time re-check of the backend-needs-bus invariant, BEFORE the
        # registries rebuild: a reload whose new manifest registers a backend while
        # the bus is unconfigured is refused here (an env-materialized backend or an
        # out-of-band manifest edit). The shared-config rule is boot-fixed env, no reload twin.
        require_bus_for_backend(manifest)
        self.start(manifest)
        run_blocking(lambda: self._run_handlers(self._epoch_handlers(), raise_on_error=True))
        # Audit the STAGED route generation before the build commits it: fail the rebuild
        # loudly if a plugin still declared in the manifest lost every one of its routes on
        # the re-import, so the atomic swap never installs a route-dropping epoch (the old
        # epoch keeps serving on the raise).
        self._audit_plugin_routes_preserved()
        return {"status": "ok"}

    def _audit_plugin_routes_preserved(self) -> None:
        """Assert the epoch rebuild kept the routes of every still-declared plugin.

        The loud guard against a silent route unmount. ``expected_owners`` is the plugin owner
        identity of each route-declaring mount binding this build resolved (a plugin
        dropped from the manifest, or bound route-less, is absent, so its legitimately-gone
        routes never trip the guard). Uses the ONE owner-identity source ``custom_route``
        stamps rows under, so the two never drift.
        """
        from tai42_skeleton.app.http import plugin_owner

        expected_owners = {
            plugin_owner(binding) for binding in (self._mount_map or {}).values() if binding.declared_routes
        }
        _lifecycle.route_registry.audit_plugin_routes_preserved(expected_owners)

    def _initialize_registries(self):
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        self._tool_registry = ToolRegistry(
            requested_tools=self._manifest.tools_list,
            tool_extensions=self._manifest.tool_extensions,
        )
        self._extension_registry = ExtensionRegistry(self._tool_registry.used_extensions)

    def _reset_component_surface(self) -> None:
        """Clear every tool / prompt / resource / resource template off the live ``local_provider``.

        Called at the top of ``start()`` (before ``_initialize_components``
        re-imports the manifest modules) so a module-level ``@tai42_app.tools.tool``
        — or any module-registered prompt / resource / resource-template — decorator
        that re-fires on the reimport always adds into a clean surface, never
        tripping ``on_duplicate="error"``.

        Enumerates the provider's OWN stored components, deliberately NOT the server
        ``list_*`` views: those filter (enabled / visibility / auth) and synthesize
        (prefab renderer resources computed on demand, resident on no provider), so
        a filtered-out component would survive the reset and re-collide on the
        re-fire, and a synthetic resource URI has nothing to remove and would raise.
        The raw provider surface is exactly the set the re-fired decorators
        (re-)populate. ``ResourceTemplate`` is a distinct component kind (not a
        ``Resource`` subclass) under its own key namespace, so it is cleared on its
        own branch. Names/URIs are de-duplicated so a versioned/unversioned mix
        cannot double-remove one name (``remove_*`` clears all versions by name in
        one call). Synchronous — a plain dict read and sync ``remove_*`` calls,
        needing no event loop, so it runs inline wherever ``start()`` runs (the
        serving loop at cold boot, a worker thread on reload).
        """
        provider = self._fast_mcp.local_provider
        components = list(provider._components.values())
        for name in {c.name for c in components if isinstance(c, Tool)}:
            provider.remove_tool(name)
        for name in {c.name for c in components if isinstance(c, Prompt)}:
            provider.remove_prompt(name)
        # ResourceTemplate is a distinct component kind, NOT a Resource subclass, so
        # the Resource branch never sweeps it — it needs its own removal branch (the
        # four kinds are disjoint, so the branch order is immaterial).
        for uri_template in {c.uri_template for c in components if isinstance(c, ResourceTemplate)}:
            provider.remove_template(uri_template)
        for uri in {str(c.uri) for c in components if isinstance(c, Resource)}:
            provider.remove_resource(uri)

    def _registry_names_sync(self) -> dict[str, set[str]]:
        """Snapshot the live server's tool / prompt / resource names off-loop.

        Used by ``start()``'s tool log. Keyed by the SINGULAR kind. Runs through the
        one off-loop ``run_blocking`` runner, safe from a loop-less caller and from
        inside the server loop.
        """

        async def snapshot() -> dict[str, set[str]]:
            return {
                "tool": {t.name for t in await self._fast_mcp.list_tools()},
                "prompt": {p.name for p in await self._fast_mcp.list_prompts()},
                "resource": {str(r.uri) for r in await self._fast_mcp.list_resources()},
            }

        return run_blocking(snapshot)
