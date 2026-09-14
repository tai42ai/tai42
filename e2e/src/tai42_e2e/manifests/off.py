"""The feature-off stack profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _pg_env, _redis_feature_env
from tai42_e2e.manifests.tool_entries import _toolbox_tools_entry
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The registry the marketplace search/detail/categories/kinds routes PROXY to. It
# is the registry CLIENT, not the install STORE, so it sits OUTSIDE the OFF gate: a
# store-less deployment still proxies. Pointed at a closed loopback port so the
# store-less proxy path is exercised hermetically — an unreachable registry maps to
# a 502 (UpstreamError), which proves the store being OFF does not gate the proxies
# without any outbound to the real default registry.
_OFF_UNREACHABLE_REGISTRY_URL = "http://127.0.0.1:9"


def build_off_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the all-features-OFF profile.

    Serves the WHOLE default router surface (``default_routers="all"``) so every
    gated feature's door is mounted, then subtracts the two config anchors that
    would resolve a store: the per-feature Redis URLs (``_redis_feature_env``) and
    the ``default`` database block (``_pg_env("TAI_DATABASE_DEFAULT_", res)``). With
    neither present, no DB-backed feature is configured, so each answers OFF: reads
    200-empty, writes 501 + ``<feature>-not-configured``, named reads 404
    byte-identical to a genuine miss, public doors uniform-404, the SSE stream 501
    before any body, ``/ready`` 200 with empty checks, ``GET /api/system/kinds`` an
    ``off`` row per feature, and exactly one rate-limit boot WARNING. Auth off; no
    backend, storage, or metrics — an absent provider is itself part of the OFF
    surface the doctrine covers.

    The web chat plugin is loaded here for its OWN store gate: its public doors carry a
    plugin-owned store (``CHANNEL_WEB_REDIS_URL``, falling back to the shared default)
    that this profile sets neither of, so the whole channel is switched off and every one
    of its doors — the visitor-facing page included — refuses 501 with its own code."""
    manifest = {
        "default_routers": "all",
        # generate_uuid gives the tool-run submit door a real tool to name (the OFF
        # store gate refuses the submit either way); api_tools off keeps the surface
        # to the mounted HTTP routers the doctrine is pinned against.
        "tools": [_toolbox_tools_entry()],
        "api_tools": {"enabled": False},
        "channel_modules": ["tai42_channel_web.register"],
    }
    env = _base_env(res, variants)
    # Subtract the two anchors that would resolve a feature store, leaving every
    # DB-backed feature genuinely unconfigured — the OFF state under test.
    for key in _redis_feature_env(res):
        env.pop(key, None)
    for key in _pg_env("TAI_DATABASE_DEFAULT_", res):
        env.pop(key, None)
    # The marketplace registry proxies are the registry CLIENT, not the install
    # store, so they are outside the OFF gate. Point them at a closed loopback port
    # so the store-less proxy path is exercised without any outbound to the real
    # default registry (an unreachable registry maps to 502, never a store refusal).
    env["MARKETPLACE_URL"] = _OFF_UNREACHABLE_REGISTRY_URL
    return StackConfig(
        name="off",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
