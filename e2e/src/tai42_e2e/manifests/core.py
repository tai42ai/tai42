"""Core stack profiles."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _TOOLBOX_EXTRA_TOOL_ENTRIES,
    _builtin_entries,
    _probe_tools_entry,
    _toolbox_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


def build_minimal_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The smallest bootable stack — one worker, no backend — for the harness
    self-tests (boot/teardown/leak-safety)."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.metrics",
            "tai42_skeleton.routers.tools",
            "tai42_skeleton.routers.config",
        ],
        # The probe entry attaches an ``e2e_http_probe: [["proxy"]]`` branch, so the
        # proxy extension module must load or startup extension-validation aborts.
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "tools": [_probe_tools_entry(with_backend_branches=False)],
        # No management surface: projection off so the mounted config/tools ops do not
        # project as MCP tools. Keeps this the smallest bootable stack.
        "api_tools": {"enabled": False},
    }
    return StackConfig(
        name="minimal",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_bare_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The full ``_CORE_ROUTERS`` surface MOUNTED but with NO storage provider and NO
    backend registered — the honest absent-provider profile the storage/backend doors'
    ``present: false`` / 501 assertions drive. One worker, no backend, auth off."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    return StackConfig(
        name="bare",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_core_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(2) + backend + metrics — the metrics round-trip / import-order
    home. Auth off."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            # The four toolbox tools not otherwise exercised (request / generate_embeddings /
            # pad_embeddings / current_time_info) load on the core profile — its tests drive
            # ``request`` against the harness target server and the embeddings tools against
            # the LLM stub's ``/v1/embeddings`` via the tool's per-call ``base_url``.
            *_TOOLBOX_EXTRA_TOOL_ENTRIES,
            {"title": "builtin-file-loader", "module": "tai42_skeleton.tools.builtin.file_loader"},
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    return StackConfig(
        name="core",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=2,
        run_backend=True,
        run_metrics=True,
        auth=False,
    )


def build_embed_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The embed deployment shape: a user-owned ``uvicorn`` host (the FastAPI app
    in ``tai42_e2e_fixtures.embed_main``) mounting ``create_app()``, plus one backend
    worker on the control-plane bus. Auth off. No ``tai metrics`` sidecar —
    ``run_metrics=False`` — because the embed app serves the in-process
    ``/metrics`` registry itself, which is the surface under test."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    return StackConfig(
        name="embed",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=True,
        run_metrics=False,
        auth=False,
        embed=True,
    )


def build_replicas_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + metrics — every cross-worker / Redis-contention test
    and the reload suite. Loads the github webhook verifier and a per-stack webhook
    secret.

    Carries no ``schedule_task`` probe branch and no scheduler process — held apart from
    the scheduling stack because on celery a ``schedule_task`` tool riding this profile's
    reload churn can leave the prefork pool unable to dispatch. Scheduling coverage lives
    on ``build_schedule_stack``."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_webhook_verifier_github", "tai42_skeleton.webhooks.builtin.shared_secret"],
        # The stripe verifier rides the canonical binding field for the webhook-verifier
        # kind; the github verifier keeps its pre-canonical ``lifecycle_modules`` slot
        # above (both loaders read either), so the two verifiers coexist on this stack.
        "webhook_verifier_modules": ["tai42_webhook_verifier_stripe"],
        # A generic deliver-only stub channel (registers on import, mounts no route) that
        # advertises form delivery, so the interactions suite can drive a channel-delivered
        # ``form`` ask (per-send data/pages) and reach its callback form page without a real
        # medium plugin.
        "channel_modules": ["tai42_e2e_fixtures.stub_channel"],
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    if res.gh_webhook_secret is not None:
        env["E2E_GH_WEBHOOK_SECRET"] = res.gh_webhook_secret
    if res.stripe_webhook_secret is not None:
        env["E2E_STRIPE_WEBHOOK_SECRET"] = res.stripe_webhook_secret
    # An external-format ``ask`` mints a callback ticket only when a public base URL
    # is set (it builds the callback URL from it); the host is never dialed, but the
    # setting requires an https value.
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    return StackConfig(
        name="replicas",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=False,
    )


def build_async_park_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, NO backend worker — the async ``ask`` park lifecycle home.

    Two replicas give the cross-worker park/resume boundary: a flow parks on replica A
    (the ``e2e_async_park_flow`` driver MCP-dispatched there binds a resume continuation
    and calls ``ask(mode="async")``), and the park is resumed on replica B — either
    by an answer through B's ``/answer`` door (which fires the stored continuation once it
    claims) or by B's expiry reaper. ``e2e_async_resume`` (the stored continuation) runs on
    both replicas, so whichever process resolves the park fires it. The expiry reaper
    interval is pinned to 1s so the expiry leg resumes promptly rather than on the 30s
    default. ``run_backend=False`` makes the module honestly ``backendless``. Auth off.

    Carries the probe tools (the single-park driver + resume continuation, and the
    multi-park super-step barrier driver + its shared continuation) plus the interactions
    router (the ``/answer`` door) and the ``ask`` builtin."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.tools",
            "tai42_skeleton.routers.config",
            "tai42_skeleton.routers.interactions",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    # An async ask park has no blocking waiter, so its continuation only fires when
    # the expiry reaper trips; pin the reaper cadence low so the expiry leg resumes in
    # seconds rather than on the 30s default.
    env["INTERACTIONS_EXPIRY_REAPER_INTERVAL_SECONDS"] = "1"
    return StackConfig(
        name="async-park",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_caller_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, no backend — the caller-ask park / kill / waiting-outcome home.

    Two replicas give the cross-worker boundary; the full core router set carries the
    tool-runs subject door (a detached run parks under a named subject), the hooks and
    schedules doors, and the interactions router, so a caller ask parked by one door is
    resumable, killable, and takeable by a later run — or another door — on the same
    subject. The expiry reaper is pinned to 1s so a kill-on-expiry or a redelivery leg
    resolves in seconds rather than on the 30s default. ``run_backend=False`` keeps the
    module honestly ``backendless``. Auth off, so the caller-ask drivers bind their own
    synthetic execution identity."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    env["INTERACTIONS_EXPIRY_REAPER_INTERVAL_SECONDS"] = "1"
    return StackConfig(
        name="caller",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_caller_cap_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The caller-ask stack with the caller concurrency cap pinned to 1 — the separate-cap leg.

    ``max_concurrent_caller`` bounds only the caller asks, so a second concurrent ``to="caller"``
    ask is refused while a ``to="user"`` ask keeps its own (default-high) ``max_concurrent`` cap.
    """
    base = build_caller_stack(res, variants)
    base.env["INTERACTIONS_MAX_CONCURRENT_CALLER"] = "1"
    return replace(base, name="caller-cap")


def build_caller_sweep_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The caller-ask stack with a short retention horizon — the untaken-outcome sweep leg.

    ``idle_ttl_seconds`` pinned low is the retention horizon: an untaken waiting outcome is dropped
    one horizon out by the reaper's retention sweep, which fires its loud
    ``interactions_outcome_dropped_untaken`` event. Kept apart from ``caller_stack`` (whose default
    horizon lets a waiting outcome linger to be taken) so the redelivery/take legs are unaffected.
    """
    base = build_caller_stack(res, variants)
    base.env["INTERACTIONS_IDLE_TTL_SECONDS"] = "5"
    return replace(base, name="caller-sweep")


def build_recycle_stack(res: StackResources, variants: Variants) -> StackConfig:
    """SUPERVISED MULTIWORKER(1) + backend — the settings-profile RECYCLE leg.

    One serve worker (the applier) plus one backend runtime on the bus, carrying the
    skeleton component store (profiles) and the sync-task probe branch (an observably
    in-flight backend job). ``supervised=True`` stamps ``TAI_SUPERVISED=harness`` into every
    child (a recycle-supported shape) and runs the harness respawn-on-exit supervisor, so a
    recycle self-exit (the applier's own deferred exit AND each orchestrated sibling) is
    re-launched and rejoins the census under a fresh origin.

    ``BACKEND_MANIFEST_KEY`` / ``BACKEND_TOOL_NAME_ARG`` are the recycle-class fields the flip
    test diffs (``reload_class="recycle"``, not in any refused tier on the ``harness`` shape).
    The recycle step budget is widened so a real worker RESPAWN boot fits inside the per-step
    census wait ``orchestrate_recycle`` allows the replacement."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    env = _base_env(res, variants)
    # A recycled worker's replacement must boot + rejoin the census within the recycle
    # orchestrator's per-step budget (``shutdown_drain_seconds``); a fresh process boot is far
    # slower than the 10s default, so widen it for this leg.
    env["TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS"] = "90"
    return StackConfig(
        name="recycle",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=True,
        run_metrics=False,
        auth=False,
        supervised=True,
    )
