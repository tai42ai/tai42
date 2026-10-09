"""The single-connection reachability probe takes every credential from the managed-auth resolver."""

from __future__ import annotations

import pytest

from tai42_skeleton.connectors.runtime.resolver import ManagedAuth
from tai42_skeleton.operations import connectors as conn_ops

from ..connectors.conftest import make_noauth_record, make_noauth_stdio_descriptor


@pytest.fixture(autouse=True)
def _connector_store_configured(monkeypatch):
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "x")


def _loader(record):
    async def _load(cid):
        return record if cid == record.connection_id else None

    return _load


async def test_noauth_probe_takes_its_credential_from_the_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-auth connection is probed with the credential the resolver returns (read-only), never with values
    the operation decrypts from the record itself."""
    record = make_noauth_record(provider_id="widgets", enabled_sub_services=["search"], config_values={"api_key": "k"})
    monkeypatch.setattr(conn_ops, "load_record_or_none", _loader(record))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_noauth_stdio_descriptor(provider_id="widgets"))
    resolved = ManagedAuth(env={"api_key": "from-resolver"})
    calls: list[tuple] = []

    async def _resolve(cid, pid, sub, *, allow_refresh):
        calls.append((cid, pid, sub, allow_refresh))
        return resolved

    probed: dict = {}

    async def _probe(descriptor, sub_service, *, auth):
        probed[sub_service] = auth
        return True

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _probe)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == []
    assert calls == [(record.connection_id, "widgets", "search", False)]
    assert probed == {"search": resolved}


async def test_noauth_probe_without_client_config_injects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-auth connection with no client config resolves to no credential and is still probed (with none)."""
    record = make_noauth_record(provider_id="widgets", enabled_sub_services=["search"], config_values={})
    monkeypatch.setattr(conn_ops, "load_record_or_none", _loader(record))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_noauth_stdio_descriptor(provider_id="widgets"))

    async def _resolve(cid, pid, sub, *, allow_refresh):
        return None

    probed: dict = {}

    async def _probe(descriptor, sub_service, *, auth):
        probed[sub_service] = auth
        return False

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _probe)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == ["search"]
    assert probed == {"search": None}
