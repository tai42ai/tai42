"""B3 — the SHIPPED google + atlassian connector modules boot and mount.

The connectors legs otherwise load only the fixture ``connector_provider``
(``manifests.py`` near the connectors profiles). This leg loads the two REAL shipped
descriptor modules (``tai42_connector.google.core.connector`` +
``tai42_connector.atlassian.core.connector``) and asserts exactly two things:

1. Descriptor registration/mount — ``GET /api/connectors/providers`` lists both
   providers with their real ``kind``/sub-services.
2. Launch-URL shape — ``POST /api/connectors/connections/start`` returns an
   ``authorize_url`` built PURELY LOCALLY from the descriptor's hardcoded authorize
   endpoint + the configured client_id + this stack's own redirect origin.

Scope ENDS there. The real descriptors hardcode the vendor authorize/token URLs with no
env indirection, so a stub IdP cannot intercept the token exchange and a probe launches
the sub-service then calls the real vendor: token/refresh/probe against live vendors is
PLAN_2's real-connector leg, deliberately NOT exercised here.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from tai42_e2e.manifests import ATLASSIAN_CLIENT_ID, GOOGLE_CLIENT_ID
from tai42_e2e.stack import TaiStack


async def test_shipped_connector_descriptors_are_registered(shipped_connectors_stack: TaiStack) -> None:
    api = shipped_connectors_stack.api(port=shipped_connectors_stack.port_a)
    catalog = await api.get("/api/connectors/providers")
    providers = {p["id"]: p for p in catalog["providers"]}

    assert "google" in providers, f"google descriptor not registered: {sorted(providers)}"
    assert "atlassian" in providers, f"atlassian descriptor not registered: {sorted(providers)}"

    google = providers["google"]
    assert google["kind"] == "oauth", google
    assert {s["id"] for s in google["sub_services"]} == {"gmail", "calendar", "drive"}, google

    atlassian = providers["atlassian"]
    assert atlassian["kind"] == "oauth", atlassian
    assert {s["id"] for s in atlassian["sub_services"]} == {"jira", "confluence", "compass"}, atlassian


async def test_google_launch_url_shape(shipped_connectors_stack: TaiStack) -> None:
    api = shipped_connectors_stack.api(port=shipped_connectors_stack.port_a)
    origin = f"http://{shipped_connectors_stack.host}:{shipped_connectors_stack.port_a}"

    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "google", "alias": "g", "enabled_sub_services": ["gmail"]},
    )
    authorize_url = start["authorize_url"]
    # The descriptor's hardcoded Google authorize endpoint — built locally, no vendor call.
    assert authorize_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"), authorize_url

    query = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
    assert query["response_type"] == "code", query
    assert query["client_id"] == GOOGLE_CLIENT_ID, query
    assert query["redirect_uri"].startswith(origin), query
    assert query["code_challenge_method"] == "S256", query
    assert query["code_challenge"], query
    assert query["state"], query
    # The gmail sub-service's granted scopes ride the scope param.
    assert "https://www.googleapis.com/auth/gmail.readonly" in query["scope"].split(" "), query


async def test_atlassian_launch_url_shape(shipped_connectors_stack: TaiStack) -> None:
    api = shipped_connectors_stack.api(port=shipped_connectors_stack.port_a)
    origin = f"http://{shipped_connectors_stack.host}:{shipped_connectors_stack.port_a}"

    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "atlassian", "alias": "a", "enabled_sub_services": ["jira"]},
    )
    authorize_url = start["authorize_url"]
    assert authorize_url.startswith("https://auth.atlassian.com/authorize?"), authorize_url

    query = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
    assert query["response_type"] == "code", query
    assert query["client_id"] == ATLASSIAN_CLIENT_ID, query
    assert query["redirect_uri"].startswith(origin), query
    assert query["code_challenge_method"] == "S256", query
    # The descriptor's ``extra_authorize_params`` — Atlassian 3LO requires these on authorize.
    assert query["audience"] == "api.atlassian.com", query
    assert query["prompt"] == "consent", query
    assert "read:issue:jira" in query["scope"].split(" "), query
