"""Every block a manifest nests refuses an unknown key, loudly, naming the key and its place.

A valid manifest touching every nested model is built once; each case plants one
misspelled key at one nested place and asserts the whole manifest is refused with
the key and its location in the error, rather than the key being dropped and the
field it was meant to set keeping its default.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from tai42_contract.manifest import Manifest

_CONNECTION_ID = "0b6f6d0e-2f6a-4f4c-9a3c-5d1e2f3a4b5c"


def _valid_manifest() -> dict[str, Any]:
    return {
        "tools": [{"title": "tools", "module": "acme.tools", "extensions": {"echo": ["chain"]}}],
        "agents": [{"title": "agents", "module": "acme.agents"}],
        "mcp": [
            {
                "title": "relay",
                "config": {"url": "http://localhost:9000/mcp", "headers": {"X-Key": "v"}},
                "managed": {"connection_id": _CONNECTION_ID, "provider_id": "acme", "sub_service": "files"},
            }
        ],
        "api_tools": {"expose_destructive": False},
        "connectors": [
            {
                "id": "acme",
                "display_name": "Acme",
                "kind": "oauth",
                "origin": "system",
                "category": "utilities",
                "oauth": {"authorize": "https://acme.example/authorize", "token": "https://acme.example/token"},
                "client_id_env": "ACME_CLIENT_ID",
                "client_secret_env": "ACME_CLIENT_SECRET",
                "sub_services": {
                    "files": {
                        "id": "files",
                        "display_name": "Files",
                        "scopes": ["files.read"],
                        "mcp_server": {"type": "http", "url": "https://acme.example/mcp"},
                    }
                },
            },
            {
                "id": "alpha",
                "display_name": "Alpha",
                "kind": "none",
                "origin": "community",
                "category": "utilities",
                "sub_services": {
                    "status": {
                        "id": "status",
                        "display_name": "Status",
                        "mcp_server": {"type": "stdio", "command": "alpha-mcp"},
                    }
                },
                "config_fields": [{"key": "api_key", "label": "API key", "target": "env"}],
            },
        ],
    }


def test_the_valid_manifest_is_accepted():
    manifest = Manifest.model_validate(_valid_manifest())
    assert manifest.api_tools.expose_destructive is False


# (path to the nested mapping, the misspelled key, the location pydantic names)
_NESTED_PLACES: list[tuple[tuple[Any, ...], str, str]] = [
    (("tools", 0), "modul", "tools.0.modul"),
    (("agents", 0), "titel", "agents.0.titel"),
    (("mcp", 0), "managd", "mcp.0.managd"),
    (("mcp", 0, "config"), "header", "mcp.0.config.header"),
    (("mcp", 0, "managed"), "sub_servic", "mcp.0.managed.sub_servic"),
    (("api_tools",), "expose_destrutive", "api_tools.expose_destrutive"),
    (("connectors", 0), "client_id_envv", "connectors.0.client_id_envv"),
    (("connectors", 0, "oauth"), "revok", "connectors.0.oauth.revok"),
    (("connectors", 0, "sub_services", "files"), "scope", "connectors.0.sub_services.files.scope"),
    (
        ("connectors", 0, "sub_services", "files", "mcp_server"),
        "extra_header",
        "connectors.0.sub_services.files.mcp_server.extra_header",
    ),
    (("connectors", 1, "config_fields", 0), "requird", "connectors.1.config_fields.0.requird"),
]


@pytest.mark.parametrize(("path", "key", "location"), _NESTED_PLACES, ids=[place[2] for place in _NESTED_PLACES])
def test_an_unknown_nested_key_is_refused_naming_it(path: tuple[Any, ...], key: str, location: str):
    data = copy.deepcopy(_valid_manifest())
    block: Any = data
    for step in path:
        block = block[step]
    block[key] = True
    with pytest.raises(ValidationError) as refused:
        Manifest.model_validate(data)
    errors = refused.value.errors()
    assert [(".".join(str(part) for part in error["loc"]), error["type"]) for error in errors] == [
        (location, "extra_forbidden")
    ], errors
    assert key in str(refused.value)


def test_a_misspelled_api_tools_key_is_refused_instead_of_leaving_destructive_exposed():
    with pytest.raises(ValidationError, match="expose_destrutive"):
        Manifest.model_validate({"api_tools": {"expose_destrutive": False}})
