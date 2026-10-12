"""Auto-reset every cached settings accessor between tests so a
``monkeypatch.setenv`` in one test can't bleed into the next via a cached value.

Does not undo the import-time constant capture in
``tools/adapters/mcp_tool_to_func.py`` — that needs a re-import, out of scope here.

Also binds the process app singleton to the ``tai42_app`` contract handle before
the test modules are imported: the router modules (imported by
``test_universal_webhook``) register their routes through the handle at import
time, exactly as external plugins do, and the runtime imports them only after
``start()`` binds the handle.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance

tai42_app.bind(instance.build_app())


@pytest.fixture(autouse=True)
def _reset_settings_caches_between_tests() -> Iterator[None]:
    reset_all_settings()
    yield
    reset_all_settings()


@pytest.fixture
def managed_providers() -> Iterator[None]:
    """The providers the managed entries of the dispatch tests reference, each entry on its provider's own server.

    ``google`` (OAuth; ``gmail`` at ``https://gmail.test/sse``) and ``acme`` (no-auth; ``api`` at
    ``https://acme.test/mcp``), so the provider's-own-server check passes them.
    """
    from tai42_contract.connectors.providers import (
        ConfigFieldSpec,
        McpServerDescriptor,
        OAuthEndpoints,
        ProviderDescriptor,
        SubServiceDescriptor,
    )

    from tai42_skeleton.connectors.providers import registry

    registry.reset_registry()
    registry.register_connector(
        ProviderDescriptor(
            id="google",
            display_name="Google",
            kind="oauth",
            origin="system",
            category="productivity",
            oauth=OAuthEndpoints(authorize="https://google.test/authorize", token="https://google.test/token"),
            client_id_env="GOOGLE_CLIENT_ID",
            client_secret_env="GOOGLE_CLIENT_SECRET",
            sub_services={
                "gmail": SubServiceDescriptor(
                    id="gmail",
                    display_name="Gmail",
                    scopes=["gmail.read"],
                    mcp_server=McpServerDescriptor(type="http", url="https://gmail.test/sse"),
                ),
            },
        )
    )
    registry.register_connector(
        ProviderDescriptor(
            id="acme",
            display_name="Acme",
            kind="none",
            origin="system",
            category="data",
            config_fields=[ConfigFieldSpec(key="x_api_key", label="Key", target="header")],
            sub_services={
                "api": SubServiceDescriptor(
                    id="api",
                    display_name="API",
                    mcp_server=McpServerDescriptor(type="http", url="https://acme.test/mcp"),
                ),
            },
        )
    )
    yield
    registry.reset_registry()
