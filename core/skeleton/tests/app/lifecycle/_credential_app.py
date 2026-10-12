"""The running app around a managed OAuth connection: its boot, and the two doors that rebuild an epoch.

The connection is :mod:`tests.connectors.credential_path`'s (a loopback MCP server, a loopback token
endpoint, a sealed record under the real resolver). The app boots with ``app_context`` on the test's
own event loop, which is the serving loop the refresh owner is bound to; a reload is driven through
``reload_config`` (the reload-gate worker thread, the swap marshalled onto the serving loop) or through
the profile apply (``ConfigService.apply_replace_env`` awaited on the serving loop), each running the
real ``build_and_swap_epoch`` whose build blocks the serving loop while it lists.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionRecord, ConnectorRef
from tai42_contract.connectors.providers import McpServerDescriptor, SubServiceDescriptor
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.connectors.service.manifest_writer import _build_entry
from tai42_skeleton.manifest import Manifest

from ..._fakes.bus import FakeBus
from ...config.fake_pipeline import FakeConfigStore, FakeReloadAdmin
from ...connectors.credential_path import CONNECTION_ID, CredentialPath

BUDGET = 1.5  # every probe budget these tests run with (boot, targeted and reload)
DOORS = ["reload_config", "profile_apply"]
OTHER_CONNECTION_ID = "44444444-4444-4444-8444-444444444444"


class AppHarness:
    """The manifest the app boots and reloads from, and the two reload doors."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: CredentialPath) -> None:
        self.path = path
        self.descriptor = path.descriptor.model_copy(
            update={
                "sub_services": {
                    **path.descriptor.sub_services,
                    # A second sub-service on the same server, for a second connection's entry.
                    "files": SubServiceDescriptor(
                        id="files",
                        display_name="Files",
                        scopes=["files.read"],
                        mcp_server=McpServerDescriptor(type="http", url=path.mcp.url),
                    ),
                }
            }
        )
        self.entries: list[TaiMCPConfig] = [path.entry]
        monkeypatch.setattr(app.config.config_manager, "read_env", dict)
        monkeypatch.setattr(app.config.config_manager, "read_manifest", self.document)

    # -- the manifest --------------------------------------------------------------------------

    def document(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "connectors": [self.descriptor.model_dump(mode="json", exclude_none=True)],
                "mcp": [entry.model_dump(mode="json", exclude_none=True) for entry in self.entries],
            }
        )

    def manifest(self) -> Manifest:
        return Manifest.model_validate(self.document())

    def other_connection(self, outcome: AuthHealthState | BaseException) -> TaiMCPConfig:
        """A second connection's entry on the same server: a record in state ``outcome``, or a failing load."""
        if isinstance(outcome, BaseException):
            self.path.others[OTHER_CONNECTION_ID] = outcome
        else:
            now = datetime.now(UTC)
            self.path.others[OTHER_CONNECTION_ID] = ConnectionRecord(
                connection_id=OTHER_CONNECTION_ID,
                provider_id="acme",
                kind="oauth",
                alias="other",
                account_identity="bob@example.com",
                enabled_sub_services=["files"],
                granted_scopes=["files.read"],
                access_token=SecretStr("at-other"),
                refresh_token=SecretStr("rt-other"),
                access_token_expires_at=now - timedelta(seconds=5),
                auth_health_state=outcome,
                created_at=now,
            )
        entry = _build_entry(
            descriptor=self.descriptor, sub_service_id="files", alias="other", connection_id=OTHER_CONNECTION_ID
        )
        self.entries.append(entry)
        return entry

    def forged(self) -> TaiMCPConfig:
        """An entry carrying the connection's reference and another address (a path the server refuses too)."""
        entry = TaiMCPConfig(
            title="elsewhere",
            config=MCPConfig(type="http", url=f"{self.path.mcp.url}/elsewhere"),
            managed=ConnectorRef(connection_id=CONNECTION_ID, provider_id="acme", sub_service="mail"),
        )
        self.entries.append(entry)
        return entry

    # -- the doors -----------------------------------------------------------------------------

    async def reload(self, door: str) -> None:
        if door == "reload_config":
            out = await reload_gate.run(app.admin.reload_config, reimports=True)
            assert out["status"] == "ok"
            return

        async def _previous(_stored: dict[str, str]) -> None:
            return None

        service = ConfigService(
            config_manager=cast("Any", FakeConfigStore(env={})),
            admin=cast("Any", FakeReloadAdmin()),
            bus=cast("Any", FakeBus()),
        )
        await service.apply_replace_env({}, save_previous=_previous)


def roomy_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift every probe budget once a test has proved it is kept, so a later listing that should bind is not
    cut by a loaded host."""
    monkeypatch.setenv("TAI_MCP_MCP_PROBE_TIMEOUT", "15")
    monkeypatch.setenv("TAI_MCP_MCP_RELOAD_PROBE_TIMEOUT", "15")
    reset_all_settings()


def bound(title: str) -> bool:
    """Whether the live epoch has ``title``'s tools bound (and no failure recorded for it)."""
    return title not in app._failed_mcps and bool(app._mcp_bound_tools.get(title))
