"""Plugin environment requirements: the env vars a plugin's items need at install.

Derived from a validated :class:`~tai42_contract.plugins.PluginSpec`: an
``mcp-server`` item's required ``!ENV`` markers and an ``oauth`` connector's
client-credential pair. The installer reads :func:`accepts_env` to decide
whether to offer an env-supply step.
"""

from __future__ import annotations

from dataclasses import dataclass

from tai42_contract.plugins import PluginItem, PluginItemKind, PluginSpec

from tai42_kit.utils.data.env_markers import EnvMarkerRef, scan_env_marker_refs


@dataclass(frozen=True)
class EnvRequirement:
    """One environment variable an installed plugin item requires.

    ``name`` is the variable name; ``secret`` marks a value the platform masks
    in views and profile snapshots. Secret-ness is a property of the requirement
    the spec DECLARES (an OAuth client secret), not of the marker grammar — a
    ``!ENV`` marker cannot know whether its value is sensitive, so marker-derived
    requirements are never secret here.
    """

    name: str
    secret: bool


def _marker_refs(item: PluginItem) -> list[EnvMarkerRef]:
    """Every ``!ENV`` marker ref carried in an item's ``mcp`` block, in document order.

    Only an ``mcp-server`` item carries markers; any other item has no ``mcp`` block and contributes none.
    """
    if item.mcp is None:
        return []
    return scan_env_marker_refs(item.mcp.model_dump(exclude_none=True))


def required_env(item: PluginItem) -> tuple[EnvRequirement, ...]:
    """The environment variables one item requires to install, in first-seen order.

    An ``mcp-server`` item requires each REQUIRED (no-default) ``!ENV`` marker
    var in its ``mcp`` block — deduped, ``secret=False`` (the marker cannot know).
    An ``oauth`` connector that names an env client requires its client-credential
    pair (``client_id_env`` non-secret, ``client_secret_env`` secret); a discovered one
    with a stored client requires nothing. A ``none``
    connector requires nothing at install (its ``config_fields`` are supplied by
    the end user at connect time), and every other kind requires nothing.
    """
    if item.kind is PluginItemKind.MCP_SERVER:
        seen: set[str] = set()
        result: list[EnvRequirement] = []
        for ref in _marker_refs(item):
            if not ref.required or ref.var in seen:
                continue
            seen.add(ref.var)
            result.append(EnvRequirement(name=ref.var, secret=False))
        return tuple(result)
    if item.kind is PluginItemKind.CONNECTOR:
        provider = item.provider
        if provider is None:
            raise ValueError(f"connector item {item.name!r} carries no provider descriptor")
        if provider.kind == "oauth":
            client_id_env = provider.client_id_env
            client_secret_env = provider.client_secret_env
            if client_id_env is None and client_secret_env is None and provider.oauth is None:
                # A discovered provider with a stored client: an administrator sets its
                # client after install, never through the environment.
                return ()
            if client_id_env is None or client_secret_env is None:
                raise ValueError(f"oauth provider {provider.id!r} is missing client_id_env/client_secret_env")
            return (
                EnvRequirement(name=client_id_env, secret=False),
                EnvRequirement(name=client_secret_env, secret=True),
            )
        return ()
    return ()


def required_env_for_spec(spec: PluginSpec) -> tuple[EnvRequirement, ...]:
    """The ordered union of every item's :func:`required_env`, keyed by name.

    First-seen order across ``spec.provides`` is preserved; when the same var is
    required by two items with different secret-ness, ``secret=True`` wins (a
    value masked by any requirement stays masked).
    """
    order: list[str] = []
    secret_by_name: dict[str, bool] = {}
    for item in spec.provides:
        for requirement in required_env(item):
            if requirement.name not in secret_by_name:
                order.append(requirement.name)
                secret_by_name[requirement.name] = requirement.secret
            else:
                secret_by_name[requirement.name] = secret_by_name[requirement.name] or requirement.secret
    return tuple(EnvRequirement(name=name, secret=secret_by_name[name]) for name in order)


def accepts_env(spec: PluginSpec) -> bool:
    """Whether the installer offers an env-supply step for this spec.

    True iff any item carries at least one ``!ENV`` marker (required OR defaulted
    — an operator may override even a defaulted marker) or the spec has a
    non-empty :func:`required_env_for_spec`. This is the installer's
    env-acceptance and routing key.
    """
    if required_env_for_spec(spec):
        return True
    return any(_marker_refs(item) for item in spec.provides)
