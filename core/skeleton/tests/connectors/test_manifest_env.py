"""The client-credential env names a raw manifest's oauth connectors reference."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_skeleton.connectors.manifest_env import ConnectorEnvRef, connector_client_env_refs


def test_oauth_entries_yield_both_names_in_order() -> None:
    manifest = {
        "connectors": [
            {"id": "a", "kind": "oauth", "client_id_env": "A_ID", "client_secret_env": "A_SECRET"},
            {"id": "b", "kind": "oauth", "client_id_env": "B_ID", "client_secret_env": "B_SECRET"},
        ]
    }
    assert connector_client_env_refs(manifest) == [
        ConnectorEnvRef(index=0, field="client_id_env", var="A_ID"),
        ConnectorEnvRef(index=0, field="client_secret_env", var="A_SECRET"),
        ConnectorEnvRef(index=1, field="client_id_env", var="B_ID"),
        ConnectorEnvRef(index=1, field="client_secret_env", var="B_SECRET"),
    ]


def test_secret_is_the_client_secret_field() -> None:
    assert ConnectorEnvRef(index=0, field="client_secret_env", var="S").secret is True
    assert ConnectorEnvRef(index=0, field="client_id_env", var="I").secret is False


def test_raw_marker_leaves_are_read_as_names() -> None:
    manifest = {"connectors": [{"kind": "oauth", "client_id_env": "!ENV ${ID_NAME}", "client_secret_env": "S"}]}
    assert [r.var for r in connector_client_env_refs(manifest)] == ["!ENV ${ID_NAME}", "S"]


def test_a_non_oauth_entry_with_names_is_ignored() -> None:
    manifest = {
        "connectors": [
            {"id": "n", "kind": "none", "client_id_env": "N_ID", "client_secret_env": "N_SECRET"},
            {"id": "o", "kind": "oauth", "client_secret_env": "O_SECRET"},
        ]
    }
    assert connector_client_env_refs(manifest) == [ConnectorEnvRef(index=1, field="client_secret_env", var="O_SECRET")]


def test_an_oauth_entry_with_no_names_contributes_nothing() -> None:
    manifest = {
        "connectors": [{"id": "o", "kind": "oauth"}, {"kind": "oauth", "client_id_env": "", "client_secret_env": 3}]
    }
    assert connector_client_env_refs(manifest) == []


@pytest.mark.parametrize(
    "manifest",
    [{}, {"connectors": None}, {"connectors": "x"}, {"connectors": {"a": 1}}, {"connectors": ["x", 3, None]}],
)
def test_a_missing_or_malformed_connectors_section_contributes_nothing(manifest: dict[str, Any]) -> None:
    assert connector_client_env_refs(manifest) == []
