"""Builders for a valid :class:`PluginSpec` and a resolve response.

The installer, the manifest patch, and the stores all consume a validated
``PluginSpec`` and the registry's resolve-response dict; these helpers produce
minimal-but-valid instances so a test states only the fields it cares about.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.plugins import PluginSpec

# The default models a COMPATIBLE plugin: the range admits ANY running contract
# version, real or test-pinned, so only tests that choose an explicit range
# exercise the compat gate.
DEFAULT_CONTRACT_RANGE = ">=0.1,<999"


def tool_item(module: str = "tai42_toolbox.tools.gen_uuid", name: str = "gen-uuid") -> dict[str, Any]:
    """A ``tool``-kind provides item."""
    return {"kind": "tool", "name": name, "module": module, "description": "Generate a UUID"}


def router_item(
    *,
    name: str = "relay",
    module: str = "tai42_relay.routes",
    base: str = "relay",
    paths: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A ``router``-kind provides item with a declared routes block.

    ``paths`` defaults to one public GET and one authed POST so a fixture carries
    both a public and an authed row; a caller states only the rows it cares about.
    """
    return {
        "kind": "router",
        "name": name,
        "module": module,
        "description": "A relay router",
        "routes": {
            "base": base,
            "paths": paths
            if paths is not None
            else [
                {"path": "/status", "methods": ["GET"], "public": True},
                {"path": "/events", "methods": ["POST"], "public": False},
            ],
        },
    }


def connector_item(
    provider_id: str = "acme",
    *,
    kind: str = "oauth",
    origin: str = "system",
    client_id_env: str | None = None,
    client_secret_env: str | None = None,
) -> dict[str, Any]:
    """A ``connector``-kind provides item carrying a ``ProviderDescriptor`` (no module).

    Abstract synthetic providers only (``acme``/``iota``/``kappa``/``relay``): an
    ``oauth`` provider names its ``client_id_env`` / ``client_secret_env`` (defaulting to
    ``<ID>_CLIENT_ID`` / ``<ID>_CLIENT_SECRET``); a ``none`` provider carries neither and
    needs no install-time env. The item ``name`` equals ``provider.id`` (the manifest /
    uninstall key). ``origin`` must be ``system`` for the ``tai42`` namespace.
    """
    if kind == "oauth":
        provider: dict[str, Any] = {
            "id": provider_id,
            "display_name": provider_id.title(),
            "icon_url": f"https://example.com/{provider_id}.png",
            "kind": "oauth",
            "origin": origin,
            "category": "productivity",
            "oauth": {"authorize": "https://auth.example.com/authorize", "token": "https://auth.example.com/token"},
            "client_id_env": client_id_env or f"{provider_id.upper()}_CLIENT_ID",
            "client_secret_env": client_secret_env or f"{provider_id.upper()}_CLIENT_SECRET",
            "sub_services": {
                "main": {
                    "id": "main",
                    "display_name": "Main",
                    "scopes": ["read"],
                    "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
                }
            },
        }
    else:  # none
        provider = {
            "id": provider_id,
            "display_name": provider_id.title(),
            "icon_url": f"https://example.com/{provider_id}.png",
            "kind": "none",
            "origin": origin,
            "category": "productivity",
            "sub_services": {
                "main": {
                    "id": "main",
                    "display_name": "Main",
                    "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
                }
            },
        }
    return {
        "kind": "connector",
        "name": provider_id,
        "provider": provider,
        "description": f"The {provider_id} connector",
    }


def make_spec(
    *,
    namespace: str = "tai42",
    name: str = "toolbox",
    package: str | None = "tai42-toolbox",
    version: str = "1.0.0",
    provides: list[dict[str, Any]] | None = None,
    contract: str = DEFAULT_CONTRACT_RANGE,
) -> PluginSpec:
    """A valid :class:`PluginSpec` with one tool item unless ``provides`` is given.

    ``package=None`` builds a descriptor-only plugin (valid only when every provides item
    is a data item — an mcp-server or a connector)."""
    document: dict[str, Any] = {
        "spec_version": 1,
        "namespace": namespace,
        "name": name,
        "version": version,
        "description": "A test plugin",
        "license": "Apache-2.0",
        "contract": contract,
        "categories": ["dev"],
        "provides": provides if provides is not None else [tool_item()],
    }
    if package is not None:
        document["package"] = package
    return PluginSpec.model_validate(document)


def make_resolved(
    spec: PluginSpec,
    *,
    source: str = "pypi",
    version: str | None = None,
    repository_url: str | None = None,
    tag: str | None = None,
    artifact_ref: str | None = None,
    sha256: str | None = None,
    advisories: list[dict[str, Any]] | None = None,
    contract_range: str | None = DEFAULT_CONTRACT_RANGE,
) -> dict[str, Any]:
    """The registry resolve-response dict the installer consumes.

    For a github source, ``artifact_ref`` defaults to a plausible codeload
    tag-tarball URL and ``sha256`` to a valid hex digest so the resolve passes the
    installer's github-provenance presence check; a test asserting the verified
    fetch fakes the download and can override either.
    """
    pinned = version if version is not None else spec.version
    default_artifact = f"https://codeload.github.com/tai42ai/{spec.name}/tar.gz/refs/tags/v{pinned}"
    return {
        "ref": spec.ref,
        "package": spec.package,
        "source": source,
        "repository_url": repository_url,
        "version": pinned,
        "tag": tag,
        "artifact_ref": artifact_ref if artifact_ref is not None else default_artifact,
        "sha256": sha256 if sha256 is not None else "0" * 64,
        "contract_range": contract_range,
        "spec": spec.model_dump(mode="json"),
        "advisories": advisories if advisories is not None else [],
    }
