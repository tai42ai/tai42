"""A reconnect re-binds, at once, the entry its dead credential left unbound.

The running app around a managed OAuth connection (``_credential_app``): the stored token expires and
its refresh fails as the user's, so a reload leaves the entry failed ``auth`` and its tools unbound. The
reconnect's completion keeps the same enabled set (no manifest change, so no reload) and must still bind
the entry before the background re-probe loop ever wakes (its sleep never returns here).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.connectors.models import AuthHealthState
from tai42_contract.connectors.service import FlowOperation

import tai42_skeleton.connectors.service.connection_service as cs
from tai42_skeleton.app.instance import app
from tai42_skeleton.connectors.oauth.client import TokenResponse
from tai42_skeleton.connectors.oauth.state import OAuthFlowState
from tai42_skeleton.connectors.service.connection_service import complete

from ...connectors import mcp_fakes
from ...connectors.credential_path import CONNECTION_ID, CredentialPath
from ._credential_app import AppHarness, bound


class _Pipeline:
    """The manifest pipeline over the harness's document; a run is recorded (none is expected here)."""

    def __init__(self, harness: AppHarness) -> None:
        self._harness = harness
        self.applies = 0

    def read_manifest_document(self) -> dict[str, Any]:
        return self._harness.document()

    async def apply_change(self, mutator: Any) -> Any:
        self.applies += 1
        raise AssertionError("a reconnect with an unchanged enabled set wrote the manifest")


def _wire_completion(monkeypatch: pytest.MonkeyPatch, harness: AppHarness, path: CredentialPath) -> _Pipeline:
    """The connection service's collaborators over the connection's sealed record."""
    pipeline = _Pipeline(harness)

    async def load_with_blob(connection_id: str, *, include_expired: bool = False):
        return path.stored(), path.store.blob

    @asynccontextmanager
    async def lock(connection_id: str):
        yield

    async def clear_cooldown(connection_id: str) -> None:
        return None

    class _Factory:
        @staticmethod
        def from_app() -> _Pipeline:
            return pipeline

    monkeypatch.setattr(cs, "load_record_with_blob", load_with_blob)
    monkeypatch.setattr(cs, "connection_lock", lock)
    monkeypatch.setattr(cs, "token_store", lambda: path.store)
    monkeypatch.setattr(cs, "clear_refresh_cooldown", clear_cooldown)
    monkeypatch.setattr(cs, "ConfigService", _Factory)
    return pipeline


def _reconnect_flow() -> OAuthFlowState:
    return OAuthFlowState(
        flow_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        provider_id="acme",
        alias="work",
        requested_scopes=["mail.read"],
        enabled_sub_services=["mail"],
        pkce_verifier="verifier",
        return_url="/connectors",
        redirect_uri="https://app.example.com/oauth-bridge.html",
        operation=FlowOperation.RECONNECT,
        reconnect_connection_id=CONNECTION_ID,
    )


def test_a_reconnect_binds_the_entry_its_dead_credential_left_unbound(monkeypatch, path):
    harness = AppHarness(monkeypatch, path)
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old", "at-new"))
    path.token_endpoint(status=400, body={"error": "invalid_grant"})
    pipeline = _wire_completion(monkeypatch, harness, path)

    async def never_wakes(_seconds: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_reprobe_sleep", never_wakes)
    title = path.entry.title

    async def run():
        async with app.app_context(harness.manifest()):
            assert bound(title)

            # The stored token expires and its refresh fails as the user's (``invalid_grant``).
            path.connect(expires_in=-5)
            await harness.reload("reload_config")
            assert not bound(title)
            assert app._failed_mcps[title]["category"] == "auth"
            assert path.stored().auth_health_state == AuthHealthState.RECONNECT_REQUIRED

            result = await complete._complete_reconnect_or_toggle(
                flow_state=_reconnect_flow(),
                descriptor=harness.descriptor,
                token_resp=TokenResponse(
                    access_token="at-new",
                    refresh_token="rt-new",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    granted_scopes=["mail.read"],
                    raw={},
                ),
                grant=path.stored().grant,
                guarded=False,
            )

            assert result.fanout is None
            assert pipeline.applies == 0
            assert path.stored().auth_health_state == AuthHealthState.HEALTHY
            assert bound(title)

    asyncio.run(run())
