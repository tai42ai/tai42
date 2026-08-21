"""B3 — the four SHIPPED connector descriptors register from the manifest and mount.

The connectors legs otherwise register only the fixture providers (built in
``tai42_e2e.manifests`` on the manifest ``connectors`` field). This leg carries the four
REAL shipped connector descriptors — google, atlassian, slack, github — on the same
manifest ``connectors`` field (read from each plugin's ``tai-plugin.yml`` at stack build),
registered SUT-side through the ``tai42_app.connectors.register_connector`` facet, and
asserts exactly two things:

1. Descriptor registration/mount — ``GET /api/connectors/providers`` lists all four
   providers with their real ``kind``/sub-services.
2. Launch-URL shape — ``POST /api/connectors/connections/start`` returns an
   ``authorize_url`` built PURELY LOCALLY from the descriptor's hardcoded authorize
   endpoint + the configured client_id + this stack's own redirect origin.

Scope ENDS there. The real descriptors hardcode the vendor authorize/token URLs with no
env indirection, so a stub IdP cannot intercept the token exchange and a probe launches
the sub-service then calls the real vendor: token/refresh/probe against live vendors is
the real-connector leg, deliberately NOT exercised here.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from tai42_e2e.manifests import ATLASSIAN_CLIENT_ID, GITHUB_CLIENT_ID, GOOGLE_CLIENT_ID, SLACK_CLIENT_ID
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# The eight Google Workspace products the google descriptor's sub-services cover.
_GOOGLE_SUB_SERVICES = {"gmail", "calendar", "drive", "docs", "sheets", "slides", "chat", "people"}
# GitHub's per-toolset sub-services.
_GITHUB_SUB_SERVICES = {
    "repos",
    "issues",
    "pull_requests",
    "actions",
    "discussions",
    "code_security",
    "secret_protection",
    "dependabot",
    "notifications",
    "orgs",
    "users",
    "gists",
}


async def test_shipped_connector_descriptors_are_registered(shipped_connectors_stack: TaiStack) -> None:
    api = shipped_connectors_stack.api(port=shipped_connectors_stack.port_a)
    catalog = await api.get("/api/connectors/providers")
    providers = {p["id"]: p for p in catalog["providers"]}

    for provider_id in ("google", "atlassian", "slack", "github"):
        assert provider_id in providers, f"{provider_id} descriptor not registered: {sorted(providers)}"

    google = providers["google"]
    assert google["kind"] == "oauth", google
    assert {s["id"] for s in google["sub_services"]} == _GOOGLE_SUB_SERVICES, google

    atlassian = providers["atlassian"]
    assert atlassian["kind"] == "oauth", atlassian
    assert {s["id"] for s in atlassian["sub_services"]} == {"jira", "confluence", "compass"}, atlassian

    slack = providers["slack"]
    assert slack["kind"] == "oauth", slack
    assert {s["id"] for s in slack["sub_services"]} == {"slack"}, slack

    github = providers["github"]
    assert github["kind"] == "oauth", github
    assert {s["id"] for s in github["sub_services"]} == _GITHUB_SUB_SERVICES, github


# The launch-URL assertion pins the fixture client_id (``GOOGLE_CLIENT_ID``); under
# TAI_E2E_REAL=connector-google the real CONNECTORS_GOOGLE_CLIENT_ID replaces it, so this is
# the connector-google mock leg — the real token/probe leg runs on the creds host.
# Inert while TAI_E2E_REAL is empty.
@pytest.mark.skipif(
    HarnessSettings().is_real("connector-google"),
    reason="fixture client_id is the connector-google mock leg; the real leg on the creds host",
)
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


# Same as the google leg: the assertion pins the fixture client_id (``ATLASSIAN_CLIENT_ID``),
# which the real CONNECTORS_ATLASSIAN_CLIENT_ID replaces under TAI_E2E_REAL=connector-atlassian.
@pytest.mark.skipif(
    HarnessSettings().is_real("connector-atlassian"),
    reason="fixture client_id is the connector-atlassian mock leg; the real leg on the creds host",
)
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


# Slack has no real leg in this suite (no ``TAI_E2E_REAL`` seam), so its client_id is always
# the fixture ``SLACK_CLIENT_ID`` and this leg always runs.
async def test_slack_launch_url_shape(shipped_connectors_stack: TaiStack) -> None:
    api = shipped_connectors_stack.api(port=shipped_connectors_stack.port_a)
    origin = f"http://{shipped_connectors_stack.host}:{shipped_connectors_stack.port_a}"

    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "slack", "alias": "s", "enabled_sub_services": ["slack"]},
    )
    authorize_url = start["authorize_url"]
    # The descriptor's hardcoded Slack authorize endpoint — built locally, no vendor call.
    assert authorize_url.startswith("https://slack.com/oauth/v2_user/authorize?"), authorize_url

    query = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
    assert query["response_type"] == "code", query
    assert query["client_id"] == SLACK_CLIENT_ID, query
    assert query["redirect_uri"].startswith(origin), query
    assert query["code_challenge_method"] == "S256", query
    assert query["state"], query
    # The slack sub-service's granted scopes ride the scope param.
    assert "search:read" in query["scope"].split(" "), query


# GitHub has no real leg in this suite either, so its client_id is always the fixture
# ``GITHUB_CLIENT_ID`` and this leg always runs.
async def test_github_launch_url_shape(shipped_connectors_stack: TaiStack) -> None:
    api = shipped_connectors_stack.api(port=shipped_connectors_stack.port_a)
    origin = f"http://{shipped_connectors_stack.host}:{shipped_connectors_stack.port_a}"

    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "github", "alias": "gh", "enabled_sub_services": ["repos"]},
    )
    authorize_url = start["authorize_url"]
    # The descriptor's hardcoded GitHub authorize endpoint — built locally, no vendor call.
    assert authorize_url.startswith("https://github.com/login/oauth/authorize?"), authorize_url

    query = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
    assert query["response_type"] == "code", query
    assert query["client_id"] == GITHUB_CLIENT_ID, query
    assert query["redirect_uri"].startswith(origin), query
    assert query["code_challenge_method"] == "S256", query
    assert query["state"], query
    # The repos sub-service's granted scope rides the scope param.
    assert "repo" in query["scope"].split(" "), query
