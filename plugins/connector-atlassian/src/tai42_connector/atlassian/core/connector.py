"""Atlassian connector provider — Jira, Confluence, and Compass.

Declares one :class:`ProviderDescriptor` and registers it through the
``tai42_app`` handle when the module loads; all OAuth/probe/launch behaviour is
generic in the engine, keyed off this descriptor.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.connectors.providers import (
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)

# Atlassian's hosted Rovo MCP endpoint, shared by all sub-services.
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"


def build_descriptor() -> ProviderDescriptor:
    """Build the Atlassian provider descriptor; the contract model validates it
    on construction, so a bad descriptor fails at load."""
    return ProviderDescriptor(
        id="atlassian",
        kind="oauth",
        origin="system",
        category="dev-tools",
        display_name="Atlassian",
        description="Connect Jira, Confluence, and Compass.",
        icon_url="/static/connector-icons/atlassian.svg",
        oauth=OAuthEndpoints(
            authorize="https://auth.atlassian.com/authorize",
            token="https://auth.atlassian.com/oauth/token",
            # Atlassian exposes no public 3LO revocation endpoint.
            revoke=None,
        ),
        client_id_env="CONNECTORS_ATLASSIAN_CLIENT_ID",
        client_secret_env="CONNECTORS_ATLASSIAN_CLIENT_SECRET",
        sub_services={
            "jira": SubServiceDescriptor(
                id="jira",
                display_name="Jira",
                description=(
                    "Read and search issues, sprints, and projects; create, update, and delete issues; update sprints."
                ),
                scopes=[
                    "offline_access",
                    # Granular scopes; Atlassian forbids mixing classic and
                    # granular scopes in one OAuth app.
                    "read:issue:jira",
                    "read:issue-meta:jira",
                    "read:project:jira",
                    "read:comment:jira",
                    "read:attachment:jira",
                    "read:issue-worklog:jira",
                    "read:jql:jira",
                    "read:board-scope:jira-software",
                    "read:sprint:jira-software",
                    "write:issue:jira",
                    "write:comment:jira",
                    "write:issue-worklog:jira",
                    "delete:issue:jira",
                    "write:sprint:jira-software",
                ],
                mcp_server=McpServerDescriptor(type="http", url=ATLASSIAN_MCP_URL),
            ),
            "confluence": SubServiceDescriptor(
                id="confluence",
                display_name="Confluence",
                description="Read and search pages, spaces, and comments; write pages.",
                scopes=[
                    "offline_access",
                    "read:page:confluence",
                    "read:hierarchical-content:confluence",
                    "read:comment:confluence",
                    "read:space:confluence",
                    "write:page:confluence",
                    # Granular equivalent of classic search:confluence (CQL content-search).
                    "read:content-details:confluence",
                ],
                mcp_server=McpServerDescriptor(type="http", url=ATLASSIAN_MCP_URL),
            ),
            "compass": SubServiceDescriptor(
                id="compass",
                display_name="Compass",
                description="Read and update components in the service catalog.",
                scopes=[
                    "offline_access",
                    "read:component:compass",
                    "write:component:compass",
                ],
                mcp_server=McpServerDescriptor(type="http", url=ATLASSIAN_MCP_URL),
            ),
        },
        # 3LO requires audience=api.atlassian.com; prompt=consent re-prompts so
        # incrementally-added sub-service scopes are granted.
        extra_authorize_params={
            "audience": "api.atlassian.com",
            "prompt": "consent",
        },
    )


# Importing this module registers the provider through the bound ``tai42_app`` handle.
tai42_app.connectors.register_connector(build_descriptor())
