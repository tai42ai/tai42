"""Tests for the Google connector descriptor and its manifest-load registration."""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.connectors.providers import ProviderDescriptor

from tai42_connector.google.core import connector
from tests import conftest

# entry_point -> scopes for each pkg-launched stdio sub-service.
EXPECTED_SUB_SERVICES = {
    "gmail": (
        "tai-mcp-google-gmail",
        [
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    ),
    "calendar": (
        "tai-mcp-google-calendar",
        [
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.events",
        ],
    ),
    "drive": (
        "tai-mcp-google-drive",
        [
            "openid",
            "email",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ],
    ),
}

# id -> (display_name, description) for each sub-service's user-facing labels.
EXPECTED_DISPLAY = {
    "gmail": ("Gmail", "List, read, and send mail."),
    "calendar": ("Calendar", "List, create, update, and delete events."),
    "drive": (
        "Drive",
        "Browse files (read-only) and create/upload app-scoped files. "
        "Pre-existing files are visible only when shared with this app "
        "via the Drive Picker.",
    ),
}


@pytest.fixture
def restore_app_binding() -> Iterator[None]:
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
        importlib.reload(connector)
        conftest.REGISTERED[:] = saved_registered


def test_sub_service_display_names_and_descriptions() -> None:
    """Pin each sub-service's user-facing ``display_name`` and ``description``."""
    descriptor = connector.build_descriptor()
    for sub_id, (display_name, description) in EXPECTED_DISPLAY.items():
        sub = descriptor.sub_services[sub_id]
        assert sub.display_name == display_name
        assert sub.description == description


def test_descriptor_constructs_and_validates() -> None:
    descriptor = connector.build_descriptor()
    assert isinstance(descriptor, ProviderDescriptor)
    assert descriptor.id == "google"
    assert descriptor.kind == "oauth"
    assert descriptor.origin == "system"
    assert descriptor.category == "communication"
    assert descriptor.display_name == "Google"
    assert descriptor.description == "Connect Gmail, Calendar, and Drive."
    assert descriptor.icon_url == "/static/connector-icons/google.svg"


def test_descriptor_passes_contract_validation() -> None:
    """Re-validating a dump re-runs every field/model validator, proving the
    descriptor is contract-valid, not merely constructible."""
    descriptor = connector.build_descriptor()
    revalidated = ProviderDescriptor.model_validate(descriptor.model_dump())
    assert revalidated == descriptor


def test_oauth_endpoints_and_client_env_names() -> None:
    descriptor = connector.build_descriptor()
    assert descriptor.oauth is not None
    assert descriptor.oauth.authorize == "https://accounts.google.com/o/oauth2/v2/auth"
    assert descriptor.oauth.token == "https://oauth2.googleapis.com/token"
    assert descriptor.oauth.revoke == "https://oauth2.googleapis.com/revoke"
    assert descriptor.client_id_env == "CONNECTORS_GOOGLE_CLIENT_ID"
    assert descriptor.client_secret_env == "CONNECTORS_GOOGLE_CLIENT_SECRET"
    assert descriptor.config_fields == []
    # ``include_granted_scopes=true`` enables incremental authorization.
    assert descriptor.extra_authorize_params == {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }


def test_pkg_launched_stdio_sub_services() -> None:
    descriptor = connector.build_descriptor()
    assert set(descriptor.sub_services) == set(EXPECTED_SUB_SERVICES)
    for sub_id, (entry_point, scopes) in EXPECTED_SUB_SERVICES.items():
        sub = descriptor.sub_services[sub_id]
        assert sub.id == sub_id
        # pkg-launched: entry_point set, no pre-built mcp_server.
        assert sub.entry_point == entry_point
        assert sub.mcp_server is None
        assert sub.scopes == scopes


def test_launch_spec_xor_holds_for_every_sub_service() -> None:
    """The contract requires exactly one of mcp_server / entry_point per
    sub-service; Google uses the entry_point (pkg-launched stdio) side."""
    descriptor = connector.build_descriptor()
    for sub in descriptor.sub_services.values():
        assert (sub.mcp_server is None) != (sub.entry_point is None)
        assert sub.entry_point is not None
        assert sub.mcp_server is None


def test_pkg_manager_present_for_entry_point_sub_services() -> None:
    """A pkg-launched (entry_point) sub-service requires the provider to name a
    launcher; ``pkg_version`` is left unset so the resolver launches latest."""
    descriptor = connector.build_descriptor()
    assert any(sub.entry_point for sub in descriptor.sub_services.values())
    assert descriptor.pkg_manager == "uvx"
    assert descriptor.pkg_version is None


def test_registration_recorded_on_import() -> None:
    """The conftest recording fake captured the import-time registration; assert
    it received exactly the descriptor the builder produces."""
    from tests.conftest import REGISTERED

    google = [d for d in REGISTERED if d.id == "google"]
    assert len(google) == 1
    assert google[0] == connector.build_descriptor()


def test_registration_invokes_handle_with_descriptor(restore_app_binding: None) -> None:
    """Reloading re-runs the module-level registration against a fresh recording
    fake. The ``restore_app_binding`` fixture undoes the rebind afterward."""
    captured: list[ProviderDescriptor] = []

    class FakeConnectors:
        def register_connector(self, descriptor: ProviderDescriptor) -> None:
            captured.append(descriptor)

    class FakeApp:
        connectors = FakeConnectors()

    tai42_app.bind(FakeApp())
    importlib.reload(connector)

    assert len(captured) == 1
    assert isinstance(captured[0], ProviderDescriptor)
    assert captured[0].id == "google"
    assert captured[0] == connector.build_descriptor()
