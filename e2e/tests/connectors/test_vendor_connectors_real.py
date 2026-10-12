"""The shipped vendor connectors against the live vendors: connect, call, refresh, disconnect.

Host-only. A vendor's leg runs only when its ``connector-<vendor>`` seam is named on
``TAI_E2E_REAL``: the operator's client credentials and the test-account marker are set, and
``E2E_PUBLIC_BASE_URL`` is the origin the vendor app's redirect is registered at, forwarding to
the stack under test. Every sign-in is a real browser consent: the leg prints the authorize
address (and writes it to ``<basetemp>/consent/``), a person signs in with the test account, the
vendor redirects to the stack's own callback page, and the leg waits for the connection to appear
(``TAI_E2E_CONSENT_TIMEOUT``).

Per vendor, on one stack built from this tree:

1. account B connects under the shipped descriptor; its served tool listing is recorded and the
   vendor's named read tool is called; its stored expiry is moved into the past and the tool
   called again (one refresh the shipped way);
2. the vendor's discovered candidate is loaded into the manifest: the shipped descriptor without
   its ``oauth`` endpoints, its env client pinned to the issuer the vendor's server names;
3. account A connects under the candidate: the login carries ``resource`` and the record's grant
   names the pinned issuer; its served tool listing is recorded and the named read tool is called
   (Atlassian: the same call's site list names ``CONNECTORS_ATLASSIAN_SITE_URL``); its stored
   expiry is moved into the past and the tool called again (one refresh, with ``resource``);
4. account B, made before the switch, is aged and called again (one refresh, without
   ``resource``: its record holds no grant);
5. both are disconnected and the upstream revoke outcome is recorded as it is.

Each step's outcome (statuses, scopes, the served tool listing, the tool called, the revoke
outcome, wall times; never a token) is written to ``<basetemp>/vendor-proof/<vendor>.json`` as the
step completes.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from mcp.types import Tool
from tai42_skeleton.connectors.service.manifest_writer import managed_title
from tai42_skeleton.tools.naming import normalized_name

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import SHIPPED_CONNECTOR_ISSUERS, discovered_candidate_descriptor
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

_SETTINGS = HarnessSettings()

# The sub-service each vendor's leg connects: one whose tools read without a site-specific argument.
_SUB_SERVICE = {"atlassian": "jira", "slack": "slack", "github": "gists"}

# The read tool each vendor's leg calls, in the vendor's own spelling: one its vendor documents as
# read-only and taking no argument. The leg looks it up under the name the platform serves it as.
# A vendor without an entry has no tool named yet: its leg records the served listing and stops
# there, naming the entry to fill from it.
_READ_TOOL = {"atlassian": "getAccessibleAtlassianResources", "github": "list_gists"}

# A vendor whose account is tied to a site: the variable naming the site the test account must
# reach. That vendor's read tool lists the sites the token reaches, so its answer is the check.
_SITE_CHECK = {"atlassian": "CONNECTORS_ATLASSIAN_SITE_URL"}

_BIND_DEADLINE = 120.0

# The bounded steps of a leg that are not waits: the tool calls, the forced refreshes, the
# manifest replace and two disconnects with their upstream revokes.
_STEPS_MARGIN = 300.0

# The leg's own pytest bound. The suite's bound is far below one browser sign-in, and a leg waits
# for two (``consent_timeout`` each), for its stack's boot when this item's setup builds it
# (``boot_timeout``), for two connections' tools to bind (``_BIND_DEADLINE`` each) and for its
# other steps (``_STEPS_MARGIN``). The bound covers the item's setup, call and teardown.
_LEG_TIMEOUT = _SETTINGS.boot_timeout + 2 * _SETTINGS.consent_timeout + 2 * _BIND_DEADLINE + _STEPS_MARGIN


def _vendor(name: str) -> Any:
    return pytest.param(
        name,
        marks=pytest.mark.skipif(
            not _SETTINGS.is_real(f"connector-{name}"),
            reason=f"live-vendor leg: runs on the creds host with TAI_E2E_REAL=connector-{name}",
        ),
        id=name,
    )


pytestmark = pytest.mark.needs(
    "kind:connectors",
    "mutable",
    "probe-tools",
    "files",
    "setting:shipped-connector-descriptors",
    "setting:CONNECTORS_REDIRECT_URI_ALLOWLIST=public-origin",
)


class _Evidence:
    """One vendor's step outcomes, rewritten to its JSON file after every step."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._steps: list[dict[str, Any]] = []
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, step: str, **facts: Any) -> None:
        self._steps.append({"step": step, "at": datetime.now(UTC).isoformat(), **facts})
        self.path.write_text(json.dumps(self._steps, indent=2, default=str) + "\n")


class _Leg:
    """The doors one vendor's leg drives on the stack."""

    def __init__(
        self, stack: TaiStack, vendor: str, consent_dir: Path, evidence: _Evidence, announce: Callable[[str], None]
    ) -> None:
        self.stack = stack
        self.vendor = vendor
        self.sub_service = _SUB_SERVICE[vendor]
        self.api: ApiClient = stack.api(port=stack.port_a)
        assert _SETTINGS.public_base_url is not None
        self.public_origin = _SETTINGS.public_base_url.rstrip("/")
        self.consent_dir = consent_dir
        self.evidence = evidence
        self.announce = announce

    async def connect(self, alias: str, account: str) -> tuple[str, dict[str, str], float]:
        """Start a login from the public origin and wait for its consent.

        Returns the connection id, the authorize query and the start call's wall time.
        """
        started_at = time.monotonic()
        started = await self.api.post(
            "/api/connectors/connections/start",
            json={"provider_id": self.vendor, "alias": alias, "enabled_sub_services": [self.sub_service]},
            headers={"Origin": self.public_origin},
            retry_on_reloading=True,
        )
        start_seconds = time.monotonic() - started_at
        authorize_url = started["authorize_url"]
        query = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        assert query["redirect_uri"].startswith(self.public_origin), query["redirect_uri"]
        self.consent_dir.mkdir(parents=True, exist_ok=True)
        (self.consent_dir / f"{alias}.url").write_text(authorize_url + "\n")
        self.announce(
            f"\n[{self.vendor}] sign in as {account} (account {alias}) within {_SETTINGS.consent_timeout:.0f}s:\n"
            f"  {authorize_url}\n"
            f"  the redirect lands on {self.public_origin}, which must forward to {self.stack.origin()}\n"
        )

        async def connected() -> dict[str, Any] | None:
            listed = await self.api.get("/api/connectors/connections")
            return next((item for item in listed["items"] if item.get("alias") == alias), None)

        found = await wait_for_async(
            connected,
            deadline=_SETTINGS.consent_timeout,
            interval=2.0,
            message=f"no {self.vendor} connection {alias!r} completed within {_SETTINGS.consent_timeout:.0f}s",
        )
        assert found["auth_health_state"] == "healthy", found
        return found["connection_id"], query, start_seconds

    async def grant(self, connection_id: str) -> dict[str, Any]:
        return await self._probe("e2e_connection_grant", {"connection_id": connection_id})

    async def age(self, connection_id: str) -> None:
        await self._probe("e2e_age_connection_token", {"connection_id": connection_id, "seconds_ago": 120})

    async def _probe(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self.stack.mcp() as mcp:
            result = await mcp.call_tool(tool, arguments, retry_on_reloading=True)
        return result.data

    def served_name(self, alias: str, tool: str) -> str:
        """The name the platform serves the connection's upstream ``tool`` under."""
        return normalized_name(managed_title(self.vendor, self.sub_service, alias), tool)

    async def named_tool(self, alias: str) -> Tool:
        """The connection's served tool the vendor's read tool is bound as.

        Waits for the connection's tools to bind and records the whole served listing (name,
        title, annotations, required arguments) before checking that the named tool is served
        and requires no argument.
        """
        prefix = normalized_name("", managed_title(self.vendor, self.sub_service, alias)) + "_"

        async def bound() -> list[Tool] | None:
            async with self.stack.mcp() as mcp:
                tools = [tool for tool in await mcp.list_tools() if tool.name.startswith(prefix)]
            return tools or None

        tools = sorted(
            await wait_for_async(
                bound, deadline=_BIND_DEADLINE, interval=1.0, message=f"no tool of {prefix}* was ever served"
            ),
            key=lambda tool: tool.name,
        )
        self.evidence.record(
            "served-tools",
            alias=alias,
            tools=[
                {
                    "name": tool.name.removeprefix(prefix),
                    "title": tool.title,
                    "annotations": None if tool.annotations is None else tool.annotations.model_dump(exclude_none=True),
                    "required": tool.inputSchema.get("required"),
                }
                for tool in tools
            ],
        )
        served = {tool.name: tool for tool in tools}
        assert self.vendor in _READ_TOOL, (
            f"{self.vendor}: no read tool is named for this vendor; name one its vendor documents as read-only "
            f"and taking no argument from the served listing recorded in {self.evidence.path} (step "
            f"'served-tools') as _READ_TOOL[{self.vendor!r}], then run the leg again"
        )
        name = self.served_name(alias, _READ_TOOL[self.vendor])
        assert name in served, f"{self.vendor}: the read tool {name!r} is not served; served: {sorted(served)}"
        required = served[name].inputSchema.get("required")
        assert not required, f"{self.vendor}: the read tool {name!r} requires arguments {required}"
        return served[name]

    async def call(self, tool: Tool) -> tuple[float, str]:
        """Call ``tool`` with no arguments; it must answer without an error. Returns the wall time and the answer."""
        started_at = time.monotonic()
        answer = await self._answer(tool.name)
        return time.monotonic() - started_at, answer

    async def _answer(self, name: str) -> str:
        async with self.stack.mcp() as mcp:
            result = await mcp.call_tool(name, {}, raise_on_error=False, retry_on_reloading=True)
        text = "".join(getattr(part, "text", "") for part in result.content)
        assert not result.is_error, f"{name} failed: {text[:500]}"
        return text

    def check_site(self, tool: Tool, answer: str) -> str | None:
        """For a vendor with a site, the read tool's answer must list the test account's site.

        Returns the site checked.
        """
        if self.vendor not in _SITE_CHECK:
            return None
        site = urlparse(os.environ[_SITE_CHECK[self.vendor]]).netloc
        assert site in answer, f"{tool.name} does not list the site {site}"
        return site

    async def load_candidate(self) -> dict[str, Any]:
        """Replace the vendor's shipped descriptor in the live manifest with its discovered candidate."""
        candidate = discovered_candidate_descriptor(self.vendor)
        document = yaml.safe_load((self.stack.root / "config" / "manifest.yml").read_text()) or {}
        document["connectors"] = [
            candidate if entry.get("id") == self.vendor else entry for entry in document.get("connectors", [])
        ]
        await self.api.post(
            "/api/manifest/replace", json={"manifest_text": yaml.safe_dump(document)}, retry_on_reloading=True
        )
        return candidate

    async def disconnect(self, connection_id: str) -> dict[str, Any]:
        result = await self.api.delete(f"/api/connectors/connections/{connection_id}", retry_on_reloading=True)
        listed = await self.api.get("/api/connectors/connections")
        assert connection_id not in {item["connection_id"] for item in listed["items"]}, listed
        return {
            "upstream_revoke_outcome": result["upstream_revoke_outcome"],
            "upstream_revoke_status": result["upstream_revoke_status"],
        }


def _in_future(expires_at: str | None) -> bool:
    return expires_at is not None and datetime.fromisoformat(expires_at) > datetime.now(UTC)


@pytest.mark.timeout(_LEG_TIMEOUT)
@pytest.mark.parametrize("vendor", [_vendor("atlassian"), _vendor("slack"), _vendor("github")])
async def test_a_vendor_moves_to_discovery_and_keeps_its_connections(
    vendor: str,
    shipped_connectors_stack: TaiStack,
    uniq: Callable[[str], str],
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shipped = next(entry for entry in shipped_connectors_stack.config.manifest["connectors"] if entry["id"] == vendor)
    assert "oauth" in shipped, f"the shipped {vendor} descriptor is already discovered: this leg proves the move"
    basetemp = tmp_path_factory.getbasetemp()
    evidence = _Evidence(basetemp / "vendor-proof" / f"{vendor}.json")

    def announce(text: str) -> None:
        with capsys.disabled():
            print(text, flush=True)

    leg = _Leg(shipped_connectors_stack, vendor, basetemp / "consent", evidence, announce)
    account = os.environ[f"CONNECTORS_{vendor.upper()}_TEST_ACCOUNT_EMAIL"]

    # 1. An account made under the shipped (declared) descriptor.
    alias_b = uniq("before")
    b_id, b_query, b_start = await leg.connect(alias_b, account)
    assert "resource" not in b_query, b_query
    b_grant = await leg.grant(b_id)
    assert b_grant["grant"] is None, b_grant
    evidence.record("connect-declared", alias=alias_b, start_seconds=round(b_start, 3), stored=b_grant)
    b_tool = await leg.named_tool(alias_b)
    b_call, _ = await leg.call(b_tool)
    await leg.age(b_id)
    b_refresh, _ = await leg.call(b_tool)
    b_after = await leg.grant(b_id)
    assert b_after["auth_health_state"] == "healthy", b_after
    assert _in_future(b_after["access_token_expires_at"]), b_after
    evidence.record(
        "refresh-declared",
        tool=b_tool.name,
        call_seconds=round(b_call, 3),
        refreshing_call_seconds=round(b_refresh, 3),
        stored=b_after,
    )

    # 2. The discovered candidate replaces the shipped descriptor.
    candidate = await leg.load_candidate()
    assert "oauth" not in candidate
    assert candidate["issuer"] == SHIPPED_CONNECTOR_ISSUERS[vendor]
    evidence.record("candidate-loaded", issuer=candidate["issuer"])

    # 3. A new account under the candidate.
    alias_a = uniq("after")
    a_id, a_query, a_start = await leg.connect(alias_a, account)
    assert a_query.get("resource"), a_query
    a_grant = await leg.grant(a_id)
    assert a_grant["grant"] is not None, a_grant
    assert a_grant["grant"]["issuer"] == SHIPPED_CONNECTOR_ISSUERS[vendor], a_grant
    assert a_grant["grant"]["resource"] == a_query["resource"], a_grant
    assert a_grant["grant"]["client_id"] is None, a_grant
    connections = {item["connection_id"]: item for item in (await leg.api.get("/api/connectors/connections"))["items"]}
    evidence.record(
        "connect-discovered",
        alias=alias_a,
        start_seconds=round(a_start, 3),
        resource=a_query["resource"],
        granted_scopes=connections[a_id]["granted_scopes"],
        stored=a_grant,
    )
    a_tool = await leg.named_tool(alias_a)
    a_call, a_answer = await leg.call(a_tool)
    site = leg.check_site(a_tool, a_answer)
    if a_grant["has_refresh_token"]:
        await leg.age(a_id)
        a_refresh, _ = await leg.call(a_tool)
        a_after = await leg.grant(a_id)
        assert a_after["auth_health_state"] == "healthy", a_after
        assert a_after["grant"] == a_grant["grant"], a_after
        assert _in_future(a_after["access_token_expires_at"]), a_after
        evidence.record(
            "refresh-discovered",
            tool=a_tool.name,
            call_seconds=round(a_call, 3),
            refreshing_call_seconds=round(a_refresh, 3),
            site=site,
            stored=a_after,
        )
    else:
        evidence.record(
            "refresh-discovered", tool=a_tool.name, call_seconds=round(a_call, 3), site=site, refresh_token=False
        )

    # 4. The account made before the switch refreshes under the candidate, still without a grant.
    await leg.age(b_id)
    b_again, _ = await leg.call(b_tool)
    b_final = await leg.grant(b_id)
    assert b_final["grant"] is None, b_final
    assert b_final["auth_health_state"] == "healthy", b_final
    assert _in_future(b_final["access_token_expires_at"]), b_final
    evidence.record("refresh-existing-connection", refreshing_call_seconds=round(b_again, 3), stored=b_final)

    # 5. Disconnect both.
    evidence.record("disconnect-discovered", **await leg.disconnect(a_id))
    evidence.record("disconnect-existing-connection", **await leg.disconnect(b_id))
