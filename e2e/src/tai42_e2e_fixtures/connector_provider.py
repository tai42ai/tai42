"""The fixture connector providers the e2e connector/fleet suites drive.

Imported SUT-side via a manifest ``lifecycle_modules`` entry; on import each
descriptor is handed to ``tai42_app.connectors.register_connector``. Four providers
are registered, all riding the one launchable managed MCP server in
``tai42_e2e_fixtures.managed_mcp_server`` (spawned self-contained via
``sys.executable -m`` — no network, no package index, no ``uvx``):

* ``e2e_idp`` — an OAuth provider whose authorize/token endpoints resolve from the
  ``E2E_IDP_*`` env the connectors profile sets. No real third party is contacted:
  the stub IdP grants and refreshes deterministically. Its ``default`` sub-service
  launches the managed server, so an OAuth-convergence scenario can call a managed
  tool over it; the refresh-lock test drives only token resolution and never calls
  the sub-service.
* ``e2e_noauth_alpha`` / ``e2e_noauth_beta`` — two DISTINCT ``kind="none"`` no-auth
  providers, each launching the same managed server. Being distinct providers they
  connect as two separate manifest records, so a concurrency scenario can connect
  both at once without a conflicting double-connect of one provider. ``beta`` also
  declares an optional ``config_values`` env field, exercising the no-auth
  config-injection channel (``reflect_env`` reads it back).
* ``e2e_noauth_multi`` — a ``kind="none"`` no-auth provider with TWO distinct
  sub-services (``alpha`` / ``beta``), each launching the same managed server, so
  each binds its own manifest entry and its own tool-prefix surface. Two enabled
  sub-services is the shape a sub-service-toggle scenario needs: one can be toggled
  OFF while the other stays enabled (satisfying the ``min_length=1`` floor on the
  patch request), and toggled back ON."""

from __future__ import annotations

import os
import sys

from tai42_contract.app import tai42_app
from tai42_contract.connectors.providers import (
    ConfigFieldSpec,
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)

_IDP_BASE = os.environ.get("E2E_IDP_BASE_URL", "http://127.0.0.1:0")

# The launch spec every fixture sub-service shares: spawn the managed MCP server module
# with the SUT's own interpreter (``sys.executable``, which already has
# ``tai42_e2e_fixtures``) — so the child launches with no network, no package index, no ``uvx``.
_MANAGED_SERVER = McpServerDescriptor(
    type="stdio",
    command=sys.executable,
    args=["-m", "tai42_e2e_fixtures.managed_mcp_server"],
)


_OAUTH_DESCRIPTOR = ProviderDescriptor(
    id="e2e_idp",
    display_name="E2E Stub IdP",
    description="Deterministic in-memory OAuth2 provider for the e2e connector tests.",
    icon_url="https://tai42.ai/e2e.png",
    kind="oauth",
    origin="community",
    category="dev-tools",
    oauth=OAuthEndpoints(authorize=f"{_IDP_BASE}/authorize", token=f"{_IDP_BASE}/token"),
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
            mcp_server=_MANAGED_SERVER,
        )
    },
    # An oauth provider must NOT declare config_fields (the contract forbids it),
    # so none are set here — the connect flow needs only the OAuth endpoints.
)


_NOAUTH_ALPHA_DESCRIPTOR = ProviderDescriptor(
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
            mcp_server=_MANAGED_SERVER,
        )
    },
)


_NOAUTH_BETA_DESCRIPTOR = ProviderDescriptor(
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
            mcp_server=_MANAGED_SERVER,
        )
    },
    # One optional client value injected on the stdio transport's env channel
    # (target must match the sub-service transport). ``reflect_env`` reads it back,
    # so a scenario can prove the no-auth config-injection path end to end.
    config_fields=[
        ConfigFieldSpec(key="e2e_beta_tag", label="Beta tag", target="env", required=False),
    ],
)


_NOAUTH_MULTI_DESCRIPTOR = ProviderDescriptor(
    id="e2e_noauth_multi",
    display_name="E2E No-Auth Multi",
    description="A no-auth managed-MCP provider with two sub-services, for the sub-service-toggle scenario.",
    icon_url="https://tai42.ai/e2e.png",
    kind="none",
    origin="community",
    category="dev-tools",
    # Two distinct sub-services, each launching the same managed server. Each enabled
    # sub-service binds its own manifest entry (titled per sub-service) with its own
    # tool prefix, so one can be toggled off while the other stays enabled — the ≥2
    # surface a sub-service-toggle scenario requires.
    sub_services={
        "alpha": SubServiceDescriptor(
            id="alpha",
            display_name="Alpha",
            description="Launches the managed MCP server; the sub-service left enabled across the toggle.",
            mcp_server=_MANAGED_SERVER,
        ),
        "beta": SubServiceDescriptor(
            id="beta",
            display_name="Beta",
            description="Launches the managed MCP server; the sub-service toggled off then back on.",
            mcp_server=_MANAGED_SERVER,
        ),
    },
)


for _descriptor in (
    _OAUTH_DESCRIPTOR,
    _NOAUTH_ALPHA_DESCRIPTOR,
    _NOAUTH_BETA_DESCRIPTOR,
    _NOAUTH_MULTI_DESCRIPTOR,
):
    tai42_app.connectors.register_connector(_descriptor)
