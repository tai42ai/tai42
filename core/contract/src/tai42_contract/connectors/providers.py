"""Provider descriptors — declarative shape of a supported third-party.

Pure pydantic models describing a provider, its sub-services, MCP server
endpoints, OAuth endpoints, and client-supplied config fields. Descriptors are
validated when built so a misconfigured provider fails loudly rather than at
first user click.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Canonical slug rule (provider_id, sub_service, config-field key): lowercase,
# must start with a letter, then alphanumerics/underscores. ``connectors.models``
# imports this — defined here (the lower layer) to avoid an import cycle.
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# -- Models ------------------------------------------------------------------


class McpServerDescriptor(BaseModel):
    """MCP server endpoint for one sub-service.

    Transport is either ``http``/``websocket`` (hosted out-of-band, set ``url``)
    or ``stdio`` (launched as a child process, set ``command`` + ``args``).
    Exactly one configuration is allowed: ``url`` xor (``command`` + ``args``).
    """

    type: Literal["http", "websocket", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not value.startswith(("http://", "https://", "ws://", "wss://")):
            raise ValueError("MCP server url must use http(s):// or ws(s):// scheme")
        return value

    def model_post_init(self, _ctx: Any) -> None:
        if self.type == "stdio":
            if not self.command:
                raise ValueError("stdio MCP server requires command")
            if self.url or self.extra_headers:
                raise ValueError("stdio MCP server must not set url/extra_headers")
        else:
            if not self.url:
                raise ValueError(f"{self.type} MCP server requires url")
            if self.command or self.args or self.env:
                raise ValueError(f"{self.type} MCP server must not set command/args/env")


class ConfigFieldSpec(BaseModel):
    """One client-supplied static config value for a no-auth provider.

    ``target`` picks the injection channel and MUST match the server transport
    (``env`` for stdio, ``header`` for http/websocket). ``secret`` flags a value
    masked in the UI and never logged; ``required`` gates connect-time validation.
    """

    key: str
    label: str = Field(min_length=1)
    target: Literal["env", "header"]
    required: bool = True
    secret: bool = False

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError("config field key must match ^[a-z][a-z0-9_]*$")
        return value


class SubServiceDescriptor(BaseModel):
    id: str
    display_name: str
    description: str = ""
    # Non-empty only for ``kind == "oauth"`` providers; a no-auth provider has no
    # scopes. Enforced at the ProviderDescriptor level (which sees ``kind``).
    scopes: list[str] = Field(default_factory=list)
    # Exactly one launch path: either a fully-declared ``mcp_server``, OR a
    # pkg-launched stdio service via ``entry_point`` (the package/console-script,
    # e.g. "tai-mcp-google-gmail") with ``mcp_server`` left unset. In the latter
    # case the engine resolver synthesizes the stdio ``McpServerDescriptor``
    # (command/args) from the provider's ``pkg_manager`` + this ``entry_point`` —
    # so a pure-data plugin never pre-declares a half-built command and the
    # ``McpServerDescriptor`` stdio-requires-command invariant stays total.
    mcp_server: McpServerDescriptor | None = None
    entry_point: str | None = None

    @field_validator("id")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError("sub-service id must match ^[a-z][a-z0-9_]*$")
        return value

    @model_validator(mode="after")
    def _check_launch_spec(self) -> SubServiceDescriptor:
        if (self.mcp_server is None) == (self.entry_point is None):
            raise ValueError(f"sub-service {self.id!r} must set exactly one of mcp_server / entry_point")
        return self


class OAuthEndpoints(BaseModel):
    authorize: str
    token: str
    revoke: str | None = None


class ProviderDescriptor(BaseModel):
    id: str
    display_name: str
    description: str = ""
    icon_url: str
    # "oauth": runs the OAuth dance; carries ``oauth`` + client envs +
    # per-sub-service scopes. "none": no-auth — no OAuth, optional client-supplied
    # ``config_fields``. Both are built by a provider plugin registered at import.
    kind: Literal["oauth", "none"]
    # Who curated the provider: "system" for shipped/ops-curated providers,
    # "community" for marketplace-contributed provider plugins. Orthogonal to
    # ``kind`` — never a combined tri-state.
    origin: Literal["system", "community"]
    # connector_category id grouping the provider in the UI. Shipped builders
    # set a seed-category literal; known-id validation happens at the
    # registration boundary, not per-field.
    category: str
    oauth: OAuthEndpoints | None = None
    client_id_env: str | None = None
    client_secret_env: str | None = None
    sub_services: dict[str, SubServiceDescriptor]
    # Provider-neutral package-launch spec for pkg-launched stdio sub-services
    # (those declaring ``entry_point`` instead of ``mcp_server``). ``pkg_manager``
    # is the launcher the engine resolver invokes; ``pkg_version`` is an optional
    # version/ref pin. The descriptor is the universal provider contract — these
    # are not ``google_``-prefixed; a provider plugin merely fills them.
    pkg_manager: Literal["uvx", "npx"] | None = None
    pkg_version: str | None = None
    # No-auth only: static values the client fills at connect time (env for stdio,
    # header for http). Empty for oauth.
    config_fields: list[ConfigFieldSpec] = Field(default_factory=list[ConfigFieldSpec])
    extra_authorize_params: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError("provider id must match ^[a-z][a-z0-9_]*$")
        return value

    @field_validator("sub_services")
    @classmethod
    def _check_sub_services(cls, value: dict[str, SubServiceDescriptor]) -> dict[str, SubServiceDescriptor]:
        if not value:
            raise ValueError("provider must declare at least one sub-service")
        for key, sub in value.items():
            if key != sub.id:
                raise ValueError(f"sub_services key {key!r} must match sub.id {sub.id!r}")
        return value

    @model_validator(mode="after")
    def _check_kind_invariants(self) -> ProviderDescriptor:
        # A pkg-launched stdio sub-service (entry_point, no mcp_server) needs the
        # provider's launcher to synthesize its command.
        if any(sub.entry_point for sub in self.sub_services.values()) and not self.pkg_manager:
            raise ValueError("provider with a pkg-launched (entry_point) sub-service requires pkg_manager")
        if self.kind == "oauth":
            if self.oauth is None:
                raise ValueError("oauth provider requires oauth endpoints")
            if not self.client_id_env or not self.client_secret_env:
                raise ValueError("oauth provider requires client_id_env + client_secret_env")
            if self.config_fields:
                raise ValueError("oauth provider must not declare config_fields")
            for sub in self.sub_services.values():
                if not sub.scopes:
                    raise ValueError(f"oauth sub-service {sub.id!r} scopes must be non-empty")
        else:  # kind == "none"
            if self.oauth is not None:
                raise ValueError("no-auth provider must not set oauth endpoints")
            if self.client_id_env or self.client_secret_env:
                raise ValueError("no-auth provider must not set client creds envs")
            # config_fields.target must match the transport channel of EVERY
            # sub-service (stdio->env, http/ws->header). A no-auth provider with
            # config_fields must therefore expose a single transport channel.
            keys = [field.key for field in self.config_fields]
            if len(keys) != len(set(keys)):
                raise ValueError("config_fields keys must be unique")
            channels = {
                "env" if (sub.mcp_server is None or sub.mcp_server.type == "stdio") else "header"
                for sub in self.sub_services.values()
            }
            if self.config_fields:
                if len(channels) != 1:
                    raise ValueError(
                        "no-auth provider with config_fields must use one transport "
                        f"channel across sub-services (got {sorted(channels)})"
                    )
                channel = next(iter(channels))
                for field in self.config_fields:
                    if field.target != channel:
                        raise ValueError(
                            f"config_field {field.key!r} target {field.target!r} must "
                            f"match the transport channel {channel!r}"
                        )
        return self
