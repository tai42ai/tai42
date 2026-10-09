"""The client-identity document's helpers: which origins it is served for, its body, the origin a request's
``Host`` names, and when a flow or a grant may use it."""

from __future__ import annotations

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import client_document as doc


@pytest.fixture
def allow(monkeypatch: pytest.MonkeyPatch):
    def _allow(origins: str, *, bridge: str | None = None, document: bool = True, name: str | None = None) -> None:
        monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", origins)
        if bridge is None:
            monkeypatch.delenv("CONNECTORS_OAUTH_BRIDGE_URL", raising=False)
        else:
            monkeypatch.setenv("CONNECTORS_OAUTH_BRIDGE_URL", bridge)
        monkeypatch.setenv("CONNECTORS_CLIENT_ID_DOCUMENT", "true" if document else "false")
        if name is not None:
            monkeypatch.setenv("CONNECTORS_CLIENT_NAME", name)
        reset_all_settings()

    return _allow


def test_the_body_field_by_field(allow):
    allow("https://app.example.com", name="Acme Assistant")
    assert doc.client_document("https://app.example.com") == {
        "client_id": "https://app.example.com/oauth/client-metadata.json",
        "client_name": "Acme Assistant",
        "client_uri": "https://app.example.com",
        "redirect_uris": ["https://app.example.com/oauth-bridge.html"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def test_the_client_id_is_the_url_the_document_is_served_at(allow):
    allow("https://app.example.com")
    body = doc.client_document("https://app.example.com")
    assert body["client_id"] == doc.client_document_url("https://app.example.com")
    assert body["client_id"] == "https://app.example.com" + doc.DOCUMENT_PATH


def test_document_origins_are_the_https_entries_in_order(allow):
    allow("http://localhost:5173,https://b.example.com,https://a.example.com,http://intranet.example.com")
    assert doc.document_origins() == ["https://b.example.com", "https://a.example.com"]


def test_the_bridge_origin_is_never_a_document_origin(allow):
    allow("https://app.example.com,https://Bridge.example.com:443", bridge="https://bridge.example.com/")
    assert doc.document_origins() == ["https://app.example.com"]
    body = doc.client_document("https://app.example.com")
    assert body["redirect_uris"] == ["https://app.example.com/oauth-bridge.html"]
    assert not doc.eligible("https://Bridge.example.com:443")


def test_one_document_origin_answers_whatever_the_host(allow):
    allow("https://app.example.com,http://localhost:5173")
    assert doc.origin_for_host("internal-proxy:8080") == "https://app.example.com"
    assert doc.origin_for_host(None) == "https://app.example.com"


def test_with_several_origins_the_host_chooses(allow):
    allow("https://a.example.com,https://b.example.com:8443")
    assert doc.origin_for_host("a.example.com") == "https://a.example.com"
    assert doc.origin_for_host("B.EXAMPLE.COM:8443") == "https://b.example.com:8443"
    assert doc.origin_for_host("b.example.com") is None
    assert doc.origin_for_host("unlisted.example.com") is None
    assert doc.origin_for_host(None) is None
    assert doc.origin_for_host("a.example.com:notaport") is None


def test_a_default_port_and_another_case_match(allow):
    allow("https://A.example.com:443,https://b.example.com")
    assert doc.origin_for_host("a.example.com") == "https://A.example.com:443"
    assert doc.origin_for_host("b.example.com:443") == "https://b.example.com"


def test_an_http_only_allow_list_has_no_document_origin(allow):
    allow("http://localhost:5173,http://intranet.example.com")
    assert doc.document_origins() == []
    assert doc.origin_for_host("intranet.example.com") is None
    assert not doc.eligible("http://intranet.example.com")


def test_eligibility_needs_the_setting_and_a_document_origin(allow):
    allow("https://app.example.com,http://localhost:5173")
    assert doc.eligible("https://app.example.com")
    assert not doc.eligible("http://localhost:5173")
    assert not doc.eligible("https://unlisted.example.com")
    allow("https://app.example.com", document=False)
    assert not doc.eligible("https://app.example.com")


def test_own_client_document_is_recognised_while_served(allow):
    url = "https://app.example.com/oauth/client-metadata.json"
    allow("https://app.example.com")
    assert doc.is_own_client_document(url)
    assert not doc.is_own_client_document("https://other.example.com/oauth/client-metadata.json")
    assert not doc.is_own_client_document("dyn-client-1")
    allow("https://app.example.com", document=False)
    assert not doc.is_own_client_document(url)
    allow("https://elsewhere.example.com")
    assert not doc.is_own_client_document(url)
