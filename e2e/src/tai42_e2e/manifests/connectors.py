"""The connector stack profiles and their descriptors."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tai42_contract.connectors.providers import (
    ConfigFieldSpec,
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)
from tai42_kit.plugins import load_plugin_spec

from tai42_e2e.manifests.feature_env import _base_env, _switch
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The stub IdP base used when a stack allocates no IdP — a syntactically valid but inert
# origin (no IdP answers it), matching the value the connect flow never reaches.
_FIXTURE_IDP_BASE_FALLBACK = "http://127.0.0.1:0"


# The launch spec every fixture sub-service shares: spawn the managed MCP server module
# with the SUT's own interpreter (``sys.executable``, which already has
# ``tai42_e2e_fixtures``) — so the child launches with no network, no package index.
_FIXTURE_MANAGED_SERVER = McpServerDescriptor(
    type="stdio",
    command=sys.executable,
    args=["-m", "tai42_e2e_fixtures.managed_mcp_server"],
)


def _fixture_connector_descriptors(idp_base: str) -> list[dict[str, object]]:
    """The four fixture provider descriptors serialized for the manifest ``connectors``
    field. ``idp_base`` stamps the OAuth provider's authorize/token endpoints at the
    stub IdP the stack allocated.

    * ``e2e_idp`` — an OAuth provider whose authorize/token endpoints resolve to the stub
      IdP. Its ``default`` sub-service launches the managed server, so an OAuth-convergence
      scenario can call a managed tool over it; the refresh-lock test drives only token
      resolution and never launches the sub-service.
    * ``e2e_noauth_alpha`` / ``e2e_noauth_beta`` — two DISTINCT ``kind="none"`` no-auth
      providers, each launching the same managed server. Being distinct providers they
      connect as two separate manifest records, so a concurrency scenario can connect both
      at once without a conflicting double-connect of one provider. ``beta`` also declares
      an optional ``config_fields`` env field, exercising the no-auth config-injection
      channel (``reflect_env`` reads it back).
    * ``e2e_noauth_multi`` — a ``kind="none"`` no-auth provider with TWO distinct
      sub-services (``alpha`` / ``beta``), each launching the same managed server, so each
      binds its own manifest entry and its own tool-prefix surface. Two enabled
      sub-services is the shape a sub-service-toggle scenario needs: one can be toggled OFF
      while the other stays enabled (satisfying the ``min_length=1`` floor on the patch
      request), and toggled back ON."""
    oauth_descriptor = ProviderDescriptor(
        id="e2e_idp",
        display_name="E2E Stub IdP",
        description="Deterministic in-memory OAuth2 provider for the e2e connector tests.",
        icon_url="https://tai42.ai/e2e.png",
        kind="oauth",
        origin="community",
        category="dev-tools",
        oauth=OAuthEndpoints(authorize=f"{idp_base}/authorize", token=f"{idp_base}/token"),
        client_id_env="E2E_IDP_CLIENT_ID",
        client_secret_env="E2E_IDP_CLIENT_SECRET",
        sub_services={
            "default": SubServiceDescriptor(
                id="default",
                display_name="Default",
                description="The single scope the refresh-lock test resolves tokens for.",
                scopes=["read"],
                # Launches the managed MCP server directly, so an OAuth-convergence
                # scenario can call a managed tool over this connection. The refresh-lock
                # test drives only token resolution and never launches the sub-service.
                mcp_server=_FIXTURE_MANAGED_SERVER,
            )
        },
        # An oauth provider must NOT declare config_fields (the contract forbids it),
        # so none are set here — the connect flow needs only the OAuth endpoints.
    )
    noauth_alpha = ProviderDescriptor(
        id="e2e_noauth_alpha",
        display_name="E2E No-Auth Alpha",
        description="A no-auth managed-MCP provider (no client config) for the fleet connect tests.",
        icon_url="https://tai42.ai/e2e.png",
        kind="none",
        origin="community",
        category="dev-tools",
        sub_services={
            "default": SubServiceDescriptor(
                id="default",
                display_name="Default",
                description="Launches the managed MCP server; no client config is required.",
                mcp_server=_FIXTURE_MANAGED_SERVER,
            )
        },
    )
    noauth_beta = ProviderDescriptor(
        id="e2e_noauth_beta",
        display_name="E2E No-Auth Beta",
        description="A second, distinct no-auth managed-MCP provider with one optional env config field.",
        icon_url="https://tai42.ai/e2e.png",
        kind="none",
        origin="community",
        category="dev-tools",
        sub_services={
            "default": SubServiceDescriptor(
                id="default",
                display_name="Default",
                description="Launches the managed MCP server; ``e2e_beta_tag`` is injected into its env.",
                mcp_server=_FIXTURE_MANAGED_SERVER,
            )
        },
        # One optional client value injected on the stdio transport's env channel
        # (target must match the sub-service transport). ``reflect_env`` reads it back,
        # so a scenario can prove the no-auth config-injection path end to end.
        config_fields=[
            ConfigFieldSpec(key="e2e_beta_tag", label="Beta tag", target="env", required=False),
        ],
    )
    noauth_multi = ProviderDescriptor(
        id="e2e_noauth_multi",
        display_name="E2E No-Auth Multi",
        description="A no-auth managed-MCP provider with two sub-services, for the sub-service-toggle scenario.",
        icon_url="https://tai42.ai/e2e.png",
        kind="none",
        origin="community",
        category="dev-tools",
        # Two distinct sub-services, each launching the same managed server. Each enabled
        # sub-service binds its own manifest entry (titled per sub-service) with its own
        # tool prefix, so one can be toggled off while the other stays enabled — the >=2
        # surface a sub-service-toggle scenario requires.
        sub_services={
            "alpha": SubServiceDescriptor(
                id="alpha",
                display_name="Alpha",
                description="Launches the managed MCP server; the sub-service left enabled across the toggle.",
                mcp_server=_FIXTURE_MANAGED_SERVER,
            ),
            "beta": SubServiceDescriptor(
                id="beta",
                display_name="Beta",
                description="Launches the managed MCP server; the sub-service toggled off then back on.",
                mcp_server=_FIXTURE_MANAGED_SERVER,
            ),
        },
    )
    return [
        descriptor.model_dump(mode="json", exclude_none=True)
        for descriptor in (oauth_descriptor, noauth_alpha, noauth_beta, noauth_multi)
    ]


def build_connectors_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, auth off — OAuth connect + refresh-lock against the stub IdP.
    Encryption keys are random per stack; the connector providers are fixtures
    carried on the manifest ``connectors`` field."""
    manifest = {
        "default_routers": "none",
        "connectors": _fixture_connector_descriptors(res.idp_base_url or _FIXTURE_IDP_BASE_FALLBACK),
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.connectors"],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # The connector store binds to the ``default`` database (skeleton component) _base_env
    # already declares, so it is live (store-configured); its Redis cache rides the shared
    # ``_redis_feature_env``.
    if res.connectors_kek is not None:
        env["CONNECTORS_KEK"] = res.connectors_kek
    if res.connectors_state_hmac_key is not None:
        env["CONNECTORS_STATE_HMAC_KEY"] = res.connectors_state_hmac_key
    if res.idp_base_url is not None:
        # The fixture OAuth provider's client credentials resolve by env name at the stub
        # IdP; its authorize/token endpoints are stamped into the manifest descriptor above.
        env["E2E_IDP_CLIENT_ID"] = "e2e-client"
        env["E2E_IDP_CLIENT_SECRET"] = "e2e-secret"
    return StackConfig(
        name="connectors",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=False,
        # The OAuth connect flow signs the deployment origin and validates it against
        # CONNECTORS_REDIRECT_URI_ALLOWLIST fail-closed; the ports are known only at boot,
        # so the stack fills this with both replicas' origins.
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
    )


# The shipped connector plugin dirs whose ``tai-plugin.yml`` descriptor each stack loads.
_SHIPPED_CONNECTOR_NAMES = ("google", "atlassian", "slack", "github")


_PLUGINS_DIR = Path(__file__).resolve().parents[4] / "plugins"


# The OAuth client credentials each shipped descriptor reads by env name
# (``client_id_env`` / ``client_secret_env``). Fixed fixture values: the client_id is
# stamped verbatim into the locally-built authorize URL the leg asserts on; the secret is
# never used (no token exchange happens on this leg), but the connect flow reads it so it
# must be present. The client_ids are PUBLIC: the shipped-connectors leg asserts the launch
# URL stamps them verbatim, so the spec exports these rather than re-hardcoding the literals.
GOOGLE_CLIENT_ID = "e2e-google-client-id"


_GOOGLE_CLIENT_SECRET = "e2e-google-client-secret"


ATLASSIAN_CLIENT_ID = "e2e-atlassian-client-id"


_ATLASSIAN_CLIENT_SECRET = "e2e-atlassian-client-secret"


SLACK_CLIENT_ID = "e2e-slack-client-id"


_SLACK_CLIENT_SECRET = "e2e-slack-client-secret"


GITHUB_CLIENT_ID = "e2e-github-client-id"


_GITHUB_CLIENT_SECRET = "e2e-github-client-secret"


def _shipped_connector_descriptors() -> list[dict[str, object]]:
    """The four shipped connector provider descriptors serialized for the manifest
    ``connectors`` field. Each is read from its plugin's ``tai-plugin.yml`` via
    ``load_plugin_spec`` and taken from ``provides[0].provider`` — the single source of
    truth for the shipped descriptor (the yml the release publishes)."""
    descriptors: list[dict[str, object]] = []
    for name in _SHIPPED_CONNECTOR_NAMES:
        spec = load_plugin_spec(_PLUGINS_DIR / f"connector-{name}" / "tai-plugin.yml")
        provider = spec.provides[0].provider
        if provider is None:
            raise RuntimeError(f"shipped connector plugin connector-{name} has no provider descriptor")
        descriptors.append(provider.model_dump(mode="json", exclude_none=True))
    return descriptors


def build_shipped_connectors_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend/metrics, auth off — the four SHIPPED connector
    descriptors (google, atlassian, slack, github) registered from the manifest
    ``connectors`` field and their launch (authorize) URLs asserted.

    Mounts the connectors router over a live connector store (bound to the ``default``
    database, so it is store-configured) with random per-stack crypto keys, and points
    ``CONNECTORS_<VENDOR>_CLIENT_ID/SECRET`` at fixed fixture values. The connect flow
    builds the authorize URL PURELY LOCALLY from the descriptor's hardcoded authorize
    endpoint + the client_id + this stack's own redirect origin (no network to the
    vendor), so the leg is hermetic. ``CONNECTORS_REDIRECT_URI_ALLOWLIST`` is filled at
    boot with this stack's own origin (the connect flow validates the request-derived
    redirect_uri against it fail-closed)."""
    manifest = {
        "default_routers": "none",
        "connectors": _shipped_connector_descriptors(),
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.connectors"],
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=False),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # The connector store binds to the ``default`` database (skeleton component) _base_env
    # already declares, so it is live (store-configured); its Redis cache rides the shared
    # ``_redis_feature_env``.
    if res.connectors_kek is not None:
        env["CONNECTORS_KEK"] = res.connectors_kek
    if res.connectors_state_hmac_key is not None:
        env["CONNECTORS_STATE_HMAC_KEY"] = res.connectors_state_hmac_key
    # The client credentials the shipped descriptors resolve by env name (the same var
    # names the operator template supplies). MOCK: fixed fixture values — only the
    # client_id is launch-URL-bearing, the secret present-but-unused on the hermetic leg.
    # REAL: the operator's live OAuth-app credentials, so the launch URL and (on the e2e
    # host) the real consent + token exchange run against the live vendor. The consent
    # round-trip itself is real-only test behavior (no mock counterpart) and needs the
    # OAuth redirect registered at the PUBLIC origin — which the connect flow validates
    # against CONNECTORS_REDIRECT_URI_ALLOWLIST. MOCK boot-fills that key from the LOOPBACK
    # origin (``origin_allowlist_env_keys``); a real connector routes it to the PUBLIC origin
    # instead via ``public_allowlist_env_keys`` (mirrors ``public_base_url_env_keys``).
    switch = _switch()
    connectors_real = switch.is_real("connector-google") or switch.is_real("connector-atlassian")
    if switch.is_real("connector-google"):
        env["CONNECTORS_GOOGLE_CLIENT_ID"] = os.environ["CONNECTORS_GOOGLE_CLIENT_ID"]
        env["CONNECTORS_GOOGLE_CLIENT_SECRET"] = os.environ["CONNECTORS_GOOGLE_CLIENT_SECRET"]
    else:
        env["CONNECTORS_GOOGLE_CLIENT_ID"] = GOOGLE_CLIENT_ID
        env["CONNECTORS_GOOGLE_CLIENT_SECRET"] = _GOOGLE_CLIENT_SECRET
    if switch.is_real("connector-atlassian"):
        env["CONNECTORS_ATLASSIAN_CLIENT_ID"] = os.environ["CONNECTORS_ATLASSIAN_CLIENT_ID"]
        env["CONNECTORS_ATLASSIAN_CLIENT_SECRET"] = os.environ["CONNECTORS_ATLASSIAN_CLIENT_SECRET"]
    else:
        env["CONNECTORS_ATLASSIAN_CLIENT_ID"] = ATLASSIAN_CLIENT_ID
        env["CONNECTORS_ATLASSIAN_CLIENT_SECRET"] = _ATLASSIAN_CLIENT_SECRET
    # Slack and github have no real leg in this suite (no ``TAI_E2E_REAL`` seam), so their
    # client credentials are always the fixed fixture values — the client_id is stamped
    # into the locally-built authorize URL, the secret present-but-unused on this leg.
    env["CONNECTORS_SLACK_CLIENT_ID"] = SLACK_CLIENT_ID
    env["CONNECTORS_SLACK_CLIENT_SECRET"] = _SLACK_CLIENT_SECRET
    env["CONNECTORS_GITHUB_CLIENT_ID"] = GITHUB_CLIENT_ID
    env["CONNECTORS_GITHUB_CLIENT_SECRET"] = _GITHUB_CLIENT_SECRET
    return StackConfig(
        name="shipped-connectors",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
        # The connect flow signs the deployment origin and validates the request-derived
        # redirect_uri against this allowlist fail-closed; the app port is known only at
        # boot, so the stack fills it with its own origin (MOCK). A real connector routes
        # the same key to the PUBLIC origin the OAuth redirect is registered at.
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
        public_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"] if connectors_real else [],
        public_base_url=switch.public_base_url,
    )
