"""Shared harness for the connection-lifecycle tests: the in-memory store/pipeline
fakes, the ``harness`` fixture wiring them onto the connection_service package, and the
token-response / flow-state / descriptor builders the operation tests reuse."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.connectors.providers import (
    McpServerDescriptor,
    ProviderDescriptor,
    SubServiceDescriptor,
)
from tai42_contract.connectors.service import AliasInUseError, FlowOperation

import tai42_skeleton.connectors.service.connection_service as cs
from tai42_skeleton.connectors.oauth.client import TokenResponse
from tai42_skeleton.connectors.oauth.state import OAuthFlowState

from ..conftest import (
    make_noauth_http_descriptor,
    make_noauth_stdio_descriptor,
    make_oauth_descriptor,
)

REDIRECT = "https://app.example.com/oauth-bridge.html"
ORIGIN = "https://app.example.com"

# The mode-wrapped fan-out summary a real ApplyResult exposes on a single-worker
# deployment (see operations._broadcast.fleet_fanout). The fake pipeline returns it
# on every apply so a writer test can assert the connector result threads it through;
# the real per-origin/unreachable shapes are exercised in tests/config/test_config_service_broadcast.py.
_FANOUT = {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"}


class _Applied:
    """The :class:`~tai42_skeleton.config.service.ApplyResult` stand-in the fake pipeline
    returns — only the ``fanout`` summary the connection service reads off it."""

    fanout = _FANOUT


class _FakeConfigService:
    """Stand-in for the manifest-mutation pipeline.

    ``apply_change`` runs the connection service's mutator against an in-memory
    PRESERVED manifest document — exactly as the real transaction hands it in —
    then records the managed titles that landed / left so a test can assert the
    pipeline both ran (``applies`` ⇒ validate + reload + broadcast) and applied the
    intended mutation. The real reload / fleet broadcast are exercised in
    ``tests/config/test_config_service_broadcast.py``.
    """

    def __init__(self, events: dict[str, list]) -> None:
        self.doc: dict[str, Any] = {"mcp": []}
        self._events = events
        self.applies = 0

    def seed(self, *, descriptor, enabled_sub_services, alias, connection_id) -> None:
        """Pre-populate the document with a connection's managed entries so a later
        remove/toggle-off can be observed leaving."""
        from tai42_skeleton.connectors.service.manifest_writer import add_managed_entries

        add_managed_entries(
            self.doc,
            descriptor=descriptor,
            enabled_sub_services=enabled_sub_services,
            alias=alias,
            connection_id=connection_id,
        )

    async def apply_change(self, mutator):
        self.applies += 1
        before = {e["title"] for e in self.doc.get("mcp") or []}
        mutator(self.doc)
        after_titles = [e["title"] for e in self.doc.get("mcp") or []]
        after = set(after_titles)
        self._events["added"].extend(title for title in after_titles if title not in before)
        self._events["removed"].extend(sorted(before - after))
        # The connection service reads its added/removed titles from the mutator's
        # captured lists; it reads only ``fanout`` off this ApplyResult stand-in.
        return _Applied()


def _blob_for(cid: str) -> bytes:
    return f"blob:{cid}".encode()


class _FakeStore:
    """Record-keyed fake with real compare-and-set + durable alias-uniqueness
    semantics on ``put``.

    ``blobs`` tracks the currently-stored ciphertext per connection; a put with
    ``expected_blob`` commits only when it matches. ``aliases`` models the
    durable ``UNIQUE (provider_id, alias)`` constraint: a create-only insert
    colliding on it raises :class:`AliasInUseError`, the store's authority."""

    def __init__(self, records) -> None:
        self.records = records
        self.blobs: dict[str, bytes] = {}
        self.aliases: dict[tuple[str | None, str | None], str] = {}
        self.puts: list = []
        self.deleted: list = []
        # Connection ids whose session has lapsed: a default (serving) load reads
        # them as missing, only an include_expired cleanup load sees them.
        self.expired: set[str] = set()

    async def put(
        self,
        connection_id,
        blob,
        *,
        create_only=False,
        expected_blob=None,
        session_expires_at=None,
        provider_id=None,
        alias=None,
    ):
        if create_only:
            owner = self.aliases.get((provider_id, alias))
            if owner is not None and owner != connection_id:
                raise AliasInUseError(f"alias {alias!r} is already in use for provider {provider_id!r}")
            self.aliases[(provider_id, alias)] = connection_id
        if expected_blob is not None and self.blobs.get(connection_id, _blob_for(connection_id)) != expected_blob:
            return False
        self.puts.append((connection_id, create_only))
        self.blobs[connection_id] = blob
        return True

    async def delete(self, connection_id):
        self.deleted.append(connection_id)
        self.blobs.pop(connection_id, None)
        # The record dict backs the fake load path too, so a delete makes a
        # subsequent load raise ConnectionNotFoundError (mirrors the real store).
        self.records.pop(connection_id, None)
        for key, owner in list(self.aliases.items()):
            if owner == connection_id:
                del self.aliases[key]

    async def list(self):
        return list(self.records)


@pytest.fixture
def harness(monkeypatch, oauth_client_env):
    records: dict[str, object] = {}
    providers: dict[str, object] = {
        "acme": make_oauth_descriptor(),
        "widgets": make_noauth_stdio_descriptor(),
        "httpsvc": make_noauth_http_descriptor(),
    }
    store = _FakeStore(records)
    flows: dict[str, OAuthFlowState] = {}
    events = {"added": [], "removed": [], "revoked": [], "state_put": []}

    monkeypatch.setattr(cs, "token_store", lambda: store)
    monkeypatch.setattr(cs, "get_provider", lambda pid: providers[pid])

    async def fake_load(cid, *, include_expired=False):
        if cid not in records or (cid in store.expired and not include_expired):
            from tai42_skeleton.connectors.store.persistence import ConnectionNotFoundError

            raise ConnectionNotFoundError(cid)
        return records[cid]

    async def fake_load_with_blob(cid, *, include_expired=False):
        return await fake_load(cid, include_expired=include_expired), store.blobs.get(cid, _blob_for(cid))

    monkeypatch.setattr(cs, "load_record", fake_load)
    monkeypatch.setattr(cs, "load_record_with_blob", fake_load_with_blob)

    @asynccontextmanager
    async def fake_lock(cid):
        yield

    monkeypatch.setattr(cs, "connection_lock", fake_lock)

    # Every writer converges through ConfigService.apply_change; inject a fake
    # pipeline that runs the real manifest_writer mutator against an in-memory
    # document. from_app returns the SAME instance so the document persists across
    # a test's operations.
    manifest_service = _FakeConfigService(events)

    class _FakeConfigServiceFactory:
        @staticmethod
        def from_app() -> _FakeConfigService:
            return manifest_service

    monkeypatch.setattr(cs, "ConfigService", _FakeConfigServiceFactory)

    # OAuth state store: in-memory.
    async def fake_state_put(flow_state):
        flows[flow_state.flow_id] = flow_state
        events["state_put"].append(flow_state.flow_id)

    async def fake_state_get_delete(flow_id):
        return flows.pop(flow_id, None)

    monkeypatch.setattr(cs.state, "put", fake_state_put)
    monkeypatch.setattr(cs.state, "get_and_delete", fake_state_get_delete)

    return cs, records, store, flows, events, providers


def _token_response(
    *,
    access_token: str = "at",
    refresh_token: str | None = "rt",
    expires_at: datetime | None = None,
    granted_scopes: list[str] | None = None,
    raw: dict | None = None,
) -> TokenResponse:
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at if expires_at is not None else datetime.now(UTC) + timedelta(hours=1),
        granted_scopes=granted_scopes if granted_scopes is not None else ["mail.read", "mail.send"],
        raw=raw if raw is not None else {},
    )


def _flow(operation=FlowOperation.CONNECT, reconnect_cid=None, redirect_uri=REDIRECT):
    return OAuthFlowState(
        flow_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        provider_id="acme",
        alias="work",
        requested_scopes=["mail.read", "mail.send"],
        enabled_sub_services=["mail"],
        pkce_verifier="verifier",
        return_url="/connectors",
        redirect_uri=redirect_uri,
        operation=operation,
        reconnect_connection_id=reconnect_cid,
    )


@asynccontextmanager
async def _depth_lock(depth: dict[str, int], cid):
    depth["n"] += 1
    try:
        yield
    finally:
        depth["n"] -= 1


def _noauth_multi_descriptor() -> ProviderDescriptor:
    """A no-auth provider with two sub-services, one declaring scopes."""
    return ProviderDescriptor(
        id="noauthmulti",
        display_name="NoAuthMulti",
        icon_url="https://noauthmulti.test/icon.png",
        kind="none",
        origin="system",
        category="data",
        sub_services={
            "main": SubServiceDescriptor(
                id="main",
                display_name="Main",
                mcp_server=McpServerDescriptor(type="http", url="https://noauthmulti.test/mcp/main"),
            ),
            "extra": SubServiceDescriptor(
                id="extra",
                display_name="Extra",
                scopes=["extra.scope"],
                mcp_server=McpServerDescriptor(type="http", url="https://noauthmulti.test/mcp/extra"),
            ),
        },
    )
