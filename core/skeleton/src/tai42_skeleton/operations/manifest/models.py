"""Request DTOs and env-key regex/constants for the manifest + MCP-status operations."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from tai42_skeleton.manifest import AgentsConfig, TaiMCPConfig, ToolsConfig

# The env var the operator's "treat these env keys as secret" marks live under — a
# comma-separated key-name list backing ``EnvSecretMarksSettings.secret_keys``. The
# secret-env door adds its generated key to this mark so the editor masks it.
_SECRET_MARKS_VAR = "TAI_ENV_SECRET_KEYS"

# A generated secret-env key is a shell identifier; a hint is sanitized to this charset.
_ENV_KEY_START = re.compile(r"[A-Za-z_]")
_NON_ENV_KEY_CHAR = re.compile(r"[^A-Za-z0-9_]+")
# An EXPLICIT key must be a full shell identifier (mirrors file_manager._ENV_KEY_RE, the
# strictest provider) — an odd value is a loud 400 before any write.
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class McpConfigUpdate(BaseModel):
    """A replacement MCP config section — the full list of MCP server entries
    that overwrites the manifest's ``mcp`` list before the reload."""

    mcp: list[TaiMCPConfig]


class McpEntriesAdd(BaseModel):
    """MCP entries to append to the manifest's ``mcp`` section. ``replace``
    lets an entry whose title already exists swap in at its current position;
    without it a title collision is refused."""

    entries: list[TaiMCPConfig]
    replace: bool = False


class ToolsEntriesAdd(BaseModel):
    """Tools entries to append to the manifest's ``tools`` section. ``replace``
    lets an entry whose title already exists swap in at its current position;
    without it a title collision is refused."""

    entries: list[ToolsConfig]
    replace: bool = False


class AgentsEntriesAdd(BaseModel):
    """Agents entries to append to the manifest's ``agents`` section. ``replace``
    lets an entry whose title already exists swap in at its current position;
    without it a title collision is refused."""

    entries: list[AgentsConfig]
    replace: bool = False


class ApiToolsListsUpdate(BaseModel):
    """Names to add to / remove from the manifest ``api_tools`` include and
    exclude lists. Each field is a bare name list; all four empty is refused."""

    include_add: list[str] = Field(default_factory=list)
    include_remove: list[str] = Field(default_factory=list)
    exclude_add: list[str] = Field(default_factory=list)
    exclude_remove: list[str] = Field(default_factory=list)


class McpTargets(BaseModel):
    """An optional fleet fan-out restriction — a ``targets`` list naming the workers
    a single-server MCP action applies to (all workers when omitted)."""

    targets: list[str] | None = None


class FailedMcpsQuery(BaseModel):
    """The failed-MCP listing door's ``?targets=`` fan-out restriction, published as an
    optional array — a repeated ``?targets=`` param a generated client sends once per worker
    (all workers when none is given).

    Spec metadata only — the door parses its query at the HTTP edge."""

    targets: list[str] = Field(
        default_factory=list,
        description="Restrict the listing to these worker names; repeat the parameter per worker, omit for the fleet.",
    )


class ManifestReplace(BaseModel):
    """A full-manifest replacement carrying the manifest TEXT verbatim — the
    PRESERVED view (``!ENV`` markers intact). The server loads it to the preserved
    document and persists it through the pipeline, so it owns resolution and no
    secret bakes to disk. There is no ``targets``: a persisted replacement reaches
    the whole fleet."""

    manifest_text: str


class SetMcpSecretEnv(BaseModel):
    """The combined env+manifest secret op body (``POST /api/mcp-config/secret-env``).

    ``value`` is the raw secret pasted by the operator. The env KEY it is stored under is
    EITHER an explicit ``key`` OR generated from ``key_hint`` — exactly one is given
    (``{value, key | key_hint, manifest_pointer}``). The server writes ``value`` to the env
    store under that key (marked secret) and writes an ``!ENV ${KEY}`` MARKER at
    ``manifest_pointer`` — so the secret lives only in the env store and the manifest carries
    a placeholder. An explicit ``key`` that collides with an existing stored key holding a
    DIFFERENT value is refused with a loud 400 naming the key (never a silent overwrite
    of a live secret). ``manifest_pointer`` is a slash-delimited, no-leading-slash path (e.g.
    ``mcp/0/config/headers/Authorization``) whose HEAD segment MUST be ``mcp`` (the same
    mcp-only authority ``set_mcp_config`` holds). The generated key is NOT returned."""

    value: str
    key: str | None = None
    key_hint: str | None = None
    manifest_pointer: str
