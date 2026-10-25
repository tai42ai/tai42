"""The wire models of the provider door: looking a server up, adding and removing a provider, and
setting a provider's client credentials; and the two catalog fields the door reads back."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import tai42_contract.connectors as connectors
from tai42_contract.connectors.models import (
    AddServerRequest,
    DiscoveredServerView,
    DiscoverServerRequest,
    ProviderCatalogEntry,
    ProviderMutationResponse,
    RemoveProviderResponse,
    SetConnectorClientRequest,
)


def _entry(**overrides: object) -> ProviderCatalogEntry:
    base: dict[str, object] = {
        "id": "remote",
        "display_name": "Remote",
        "kind": "oauth",
        "origin": "community",
        "category": "other",
        "sub_services": [],
    }
    base.update(overrides)
    return ProviderCatalogEntry.model_validate(base)


def test_a_look_up_names_an_address_or_a_provider() -> None:
    assert DiscoverServerRequest(url="https://mcp.example.com/mcp").provider_id is None
    assert DiscoverServerRequest(provider_id="remote").url is None


@pytest.mark.parametrize("body", [{}, {"url": "https://mcp.example.com/mcp", "provider_id": "remote"}])
def test_a_look_up_names_exactly_one_of_the_two(body: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="exactly one of url or provider_id"):
        DiscoverServerRequest.model_validate(body)


def test_the_discovered_view_round_trips() -> None:
    view = DiscoveredServerView(
        url="https://mcp.example.com/mcp",
        resource="https://mcp.example.com/mcp",
        resource_name="Example",
        issuer="https://auth.example.com",
        authorization_servers=["https://auth.example.com", "https://other.example.com"],
        client_issuer=None,
        registration_supported=True,
        automatic_client=True,
        scopes_supported=["read"],
        suggested_id="mcp_example_com",
        suggested_display_name="Example",
        redirect_uri="https://app.example.com/oauth-bridge.html",
    )
    assert DiscoveredServerView.model_validate(view.model_dump(mode="json")) == view


def test_an_add_request_defaults() -> None:
    request = AddServerRequest(url="https://mcp.example.com/mcp")
    assert request.id is None
    assert request.display_name is None
    assert request.description == ""
    assert request.icon_url is None
    assert request.category is None
    assert request.scopes == []


def test_an_add_request_refuses_an_id_that_is_not_a_slug() -> None:
    with pytest.raises(ValidationError):
        AddServerRequest(url="https://mcp.example.com/mcp", id="Not-A-Slug")


def test_the_client_secret_is_never_shown() -> None:
    request = SetConnectorClientRequest(client_id="app", client_secret="s3cr3t-value")  # type: ignore[arg-type]
    assert request.client_secret is not None
    assert request.client_secret.get_secret_value() == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(request)
    assert "s3cr3t-value" not in request.model_dump_json()
    assert request.token_endpoint_auth_method is None
    assert request.issuer is None


def test_a_client_request_needs_a_client_id_and_a_known_method() -> None:
    with pytest.raises(ValidationError):
        SetConnectorClientRequest(client_id="")
    with pytest.raises(ValidationError):
        SetConnectorClientRequest(client_id="app", token_endpoint_auth_method="private_key_jwt")  # type: ignore[arg-type]


def test_the_mutation_responses() -> None:
    added = ProviderMutationResponse(provider=_entry(removable=True, client_source="unset"), fanout=None)
    assert added.provider.removable is True
    removed = RemoveProviderResponse(provider_id="remote", removed_clients=2, fanout={"mode": "local-only"})
    assert removed.model_dump(mode="json") == {
        "provider_id": "remote",
        "removed_clients": 2,
        "fanout": {"mode": "local-only"},
        "span_release_error": None,
    }
    held = RemoveProviderResponse(
        provider_id="remote",
        removed_clients=0,
        fanout={"mode": "local-only"},
        span_release_error="OSError: releasing the span failed",
    )
    assert held.model_dump(mode="json")["span_release_error"] == "OSError: releasing the span failed"


def test_a_catalog_entry_says_whether_it_is_removable_and_where_its_client_comes_from() -> None:
    entry = _entry()
    assert entry.removable is False
    assert entry.client_source is None
    for source in ("env", "manual", "dynamic", "unset"):
        assert _entry(client_source=source).client_source == source
    with pytest.raises(ValidationError):
        _entry(client_source="document")


def test_the_door_models_are_exported() -> None:
    for name in (
        "AddServerRequest",
        "DiscoveredServerView",
        "DiscoverServerRequest",
        "ProviderMutationResponse",
        "RemoveProviderResponse",
        "SetConnectorClientRequest",
    ):
        assert name in connectors.__all__
        assert getattr(connectors, name) is not None
