"""Cached accessors for the core settings values read hot across the process."""

from tai42_kit.settings import settings_cache

from tai42_skeleton.settings.settings import (
    AppArgsSettings,
    CoreSettings,
)


@settings_cache
def manifest_path() -> str | None:
    """The configured manifest path, or ``None`` when unset."""
    return CoreSettings().manifest_path


@settings_cache
def backend_provider() -> str:
    """The configured execution backend provider name, lowercased."""
    return (CoreSettings().backend or "").strip().lower()


@settings_cache
def template_provider() -> str:
    """The configured template provider name, lowercased."""
    return (CoreSettings().template or "").strip().lower()


@settings_cache
def sandbox_provider() -> str:
    """The configured sandbox provider name, lowercased."""
    return (CoreSettings().sandbox or "").strip().lower()


@settings_cache
def mcp_probe_timeout() -> float:
    """The MCP server start budget in seconds: how long a server start may take before it is torn down."""
    return CoreSettings().mcp_probe_timeout


@settings_cache
def mcp_reload_probe_timeout() -> float:
    """How long a reload waits for an MCP server start, in seconds; the start itself goes on to the start budget."""
    return CoreSettings().mcp_reload_probe_timeout


@settings_cache
def app_args_settings() -> AppArgsSettings:
    """The parsed app-args settings group."""
    return AppArgsSettings()
