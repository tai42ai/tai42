"""Fixtures of the lifecycle tests: for the credential path, a loopback MCP server, a loopback token endpoint, and
the managed OAuth connection wired between them under the real resolver (see ``tests.connectors.credential_path``);
for the MCP server start tests, a rebuild whose reload wait is far shorter than the start budget."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import lifecycle as lifecycle_module

from ...connectors import mcp_fakes
from ...connectors.credential_path import CredentialPath
from ...connectors.oauth_fakes import FakeOAuthServer
from ...connectors.oauth_fakes import serve as serve_oauth
from ._credential_app import BUDGET


@pytest.fixture(scope="module")
def mcp_server() -> Iterator[mcp_fakes.FakeMcpServer]:
    yield from mcp_fakes.serve()


@pytest.fixture(scope="module")
def oauth_server() -> Iterator[FakeOAuthServer]:
    yield from serve_oauth()


@pytest.fixture
def path(monkeypatch: pytest.MonkeyPatch, mcp_server, oauth_server) -> Iterator[CredentialPath]:
    """The connection, every probe budget set to ``BUDGET``; no refresh owner bound."""
    from tai42_skeleton.connectors.runtime import refresh_owner

    monkeypatch.setenv("TAI_MCP_MCP_PROBE_TIMEOUT", str(BUDGET))
    monkeypatch.setenv("TAI_MCP_MCP_RELOAD_PROBE_TIMEOUT", str(BUDGET))
    wiring = CredentialPath(monkeypatch, mcp_server, oauth_server)
    reset_all_settings()
    yield wiring
    refresh_owner.reset()
    mcp_server.reset()


@pytest.fixture
def short_reload_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rebuild in progress whose reload wait (0.1s) is far shorter than the start budget (10s).

    A start the test leaves running is a task of the loop that launched it, so it ends with that loop's close.
    """
    monkeypatch.setenv("TAI_MCP_MCP_PROBE_TIMEOUT", "10")
    monkeypatch.setenv("TAI_MCP_MCP_RELOAD_PROBE_TIMEOUT", "0.1")
    reset_all_settings()
    monkeypatch.setattr(lifecycle_module, "is_epoch_rebuild_in_progress", lambda: True)
