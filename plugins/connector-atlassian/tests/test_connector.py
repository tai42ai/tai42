"""Tests for the Atlassian connector provider."""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.connectors.providers import ProviderDescriptor

from tai42_connector.atlassian.core import connector as connector_mod
from tests import conftest

ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"


@pytest.fixture
def restore_tai_app() -> Iterator[None]:
    """Snapshot the bound app impl and recorded registrations, then restore both
    and reload the connector on teardown so a test's rebind does not leak into
    later tests. The impl is read via ``object.__getattribute__`` — the public
    handle exposes no accessor for its ``_impl`` slot."""
    saved_impl = object.__getattribute__(tai42_app, "_impl")
    saved_registered = list(conftest.REGISTERED)
    try:
        yield
    finally:
        tai42_app.bind(saved_impl)
        importlib.reload(connector_mod)
        conftest.REGISTERED[:] = saved_registered


def test_descriptor_constructs_and_validates() -> None:
    descriptor = connector_mod.build_descriptor()

    assert isinstance(descriptor, ProviderDescriptor)
    assert descriptor.id == "atlassian"
    assert descriptor.kind == "oauth"
    assert descriptor.origin == "system"
    assert descriptor.category == "dev-tools"
    assert descriptor.display_name == "Atlassian"
    assert descriptor.description == "Connect Jira, Confluence, and Compass."
    assert descriptor.icon_url == "/static/connector-icons/atlassian.svg"

    assert descriptor.oauth is not None
    assert descriptor.oauth.authorize == "https://auth.atlassian.com/authorize"
    assert descriptor.oauth.token == "https://auth.atlassian.com/oauth/token"
    assert descriptor.oauth.revoke is None

    assert descriptor.client_id_env == "CONNECTORS_ATLASSIAN_CLIENT_ID"
    assert descriptor.client_secret_env == "CONNECTORS_ATLASSIAN_CLIENT_SECRET"

    assert descriptor.extra_authorize_params == {
        "audience": "api.atlassian.com",
        "prompt": "consent",
    }


def test_descriptor_passes_contract_validation() -> None:
    """Re-validating a dump re-runs every field/model validator, proving the
    descriptor is contract-valid, not merely constructible."""
    descriptor = connector_mod.build_descriptor()
    revalidated = ProviderDescriptor.model_validate(descriptor.model_dump())
    assert revalidated == descriptor


def test_sub_services() -> None:
    descriptor = connector_mod.build_descriptor()

    assert set(descriptor.sub_services) == {"jira", "confluence", "compass"}

    assert descriptor.sub_services["jira"].display_name == "Jira"
    assert (
        descriptor.sub_services["jira"].description
        == "Read and search issues, sprints, and projects; create, update, and delete issues; update sprints."
    )
    assert descriptor.sub_services["confluence"].display_name == "Confluence"
    assert (
        descriptor.sub_services["confluence"].description == "Read and search pages, spaces, and comments; write pages."
    )
    assert descriptor.sub_services["compass"].display_name == "Compass"
    assert descriptor.sub_services["compass"].description == "Read and update components in the service catalog."

    for sub_id, sub in descriptor.sub_services.items():
        assert sub.id == sub_id
        assert sub.scopes  # oauth sub-services must declare scopes
        assert "offline_access" in sub.scopes
        assert sub.entry_point is None
        assert sub.mcp_server is not None
        assert sub.mcp_server.type == "http"
        assert sub.mcp_server.url == ATLASSIAN_MCP_URL

    assert descriptor.sub_services["jira"].scopes == [
        "offline_access",
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
    ]
    assert descriptor.sub_services["confluence"].scopes == [
        "offline_access",
        "read:page:confluence",
        "read:hierarchical-content:confluence",
        "read:comment:confluence",
        "read:space:confluence",
        "write:page:confluence",
        "read:content-details:confluence",
    ]
    assert descriptor.sub_services["compass"].scopes == [
        "offline_access",
        "read:component:compass",
        "write:component:compass",
    ]


def test_launch_spec_xor_holds_for_every_sub_service() -> None:
    """The contract requires exactly one of mcp_server / entry_point per
    sub-service; Atlassian uses the hosted http ``mcp_server`` side, so no
    pkg launcher is declared."""
    descriptor = connector_mod.build_descriptor()
    for sub in descriptor.sub_services.values():
        assert (sub.mcp_server is None) != (sub.entry_point is None)
        assert sub.mcp_server is not None
        assert sub.entry_point is None
    assert descriptor.pkg_manager is None
    assert descriptor.pkg_version is None
    assert descriptor.config_fields == []


def test_registration_recorded_on_import() -> None:
    """The conftest recording fake captured the import-time registration; assert
    it received exactly the descriptor the builder produces."""
    from tests.conftest import REGISTERED

    atlassian = [d for d in REGISTERED if d.id == "atlassian"]
    assert len(atlassian) == 1
    assert atlassian[0] == connector_mod.build_descriptor()


def test_registration_invokes_handle(restore_tai_app: None) -> None:
    captured: list[ProviderDescriptor] = []

    class FakeConnectors:
        def register_connector(self, descriptor: ProviderDescriptor) -> None:
            captured.append(descriptor)

    class FakeApp:
        connectors = FakeConnectors()

    tai42_app.bind(FakeApp())
    # Reloading re-runs the module-level registration against the fake handle.
    importlib.reload(connector_mod)

    assert len(captured) == 1
    assert isinstance(captured[0], ProviderDescriptor)
    assert captured[0].id == "atlassian"
    assert captured[0] == connector_mod.build_descriptor()
