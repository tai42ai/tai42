"""Google connector provider — Gmail, Calendar, and Drive.

Declares one :class:`~tai42_contract.connectors.providers.ProviderDescriptor` and
registers it through the ``tai42_app`` handle when the module loads; all
OAuth/probe/launch behaviour is generic in the engine, keyed off the descriptor.
Each sub-service is a pkg-launched stdio MCP server named by its ``entry_point``.

Keep Drive scoped to ``drive.readonly`` + ``drive.file`` — never the full ``drive``
scope, which would expose the user's entire Drive.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.connectors.providers import (
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)


def build_descriptor() -> ProviderDescriptor:
    """Build the pure Google provider descriptor (no engine, no side effects)."""
    return ProviderDescriptor(
        id="google",
        kind="oauth",
        origin="system",
        category="communication",
        display_name="Google",
        description="Connect Gmail, Calendar, and Drive.",
        icon_url="/static/connector-icons/google.svg",
        oauth=OAuthEndpoints(
            authorize="https://accounts.google.com/o/oauth2/v2/auth",
            token="https://oauth2.googleapis.com/token",
            revoke="https://oauth2.googleapis.com/revoke",
        ),
        client_id_env="CONNECTORS_GOOGLE_CLIENT_ID",
        client_secret_env="CONNECTORS_GOOGLE_CLIENT_SECRET",
        # Sub-services run as stdio MCP-server processes via uvx; unset
        # ``pkg_version`` means the resolver launches the latest.
        pkg_manager="uvx",
        sub_services={
            "gmail": SubServiceDescriptor(
                id="gmail",
                display_name="Gmail",
                description="List, read, and send mail.",
                scopes=[
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.send",
                ],
                entry_point="tai-mcp-google-gmail",
            ),
            "calendar": SubServiceDescriptor(
                id="calendar",
                display_name="Calendar",
                description="List, create, update, and delete events.",
                scopes=[
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/calendar.events",
                ],
                entry_point="tai-mcp-google-calendar",
            ),
            "drive": SubServiceDescriptor(
                id="drive",
                display_name="Drive",
                description=(
                    "Browse files (read-only) and create/upload app-scoped files. "
                    "Pre-existing files are visible only when shared with this app "
                    "via the Drive Picker."
                ),
                scopes=[
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/drive.file",
                ],
                entry_point="tai-mcp-google-drive",
            ),
        },
        # ``access_type``/``prompt`` force a refresh token on first consent;
        # ``include_granted_scopes`` enables incremental authorization.
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    )


# Importing this module registers the provider through the bound ``tai42_app`` handle.
tai42_app.connectors.register_connector(build_descriptor())
