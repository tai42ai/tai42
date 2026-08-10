"""Connector door hardening.

A syntactically malformed connection id (not a UUID) keys no record, so the read and
delete doors answer a plain 404 — never a 500 that would leak the store's shape as an
oracle. And a request-body validation failure returns the failing field PATHS with the
submitted VALUES stripped (N4): a rejected ``config_values`` secret must never ride the
400 body.
"""

from __future__ import annotations

from tai42_e2e.stack import TaiStack

# Not a UUID — the store rejects it before any lookup, so it is indistinguishable from a
# genuine miss (both 404).
_MALFORMED_ID = "not-a-valid-uuid"


async def test_malformed_connection_id_is_404_on_read_and_delete(connectors_stack: TaiStack) -> None:
    api = connectors_stack.api(port=connectors_stack.port_a)

    read = await api.request_raw("GET", f"/api/connectors/connections/{_MALFORMED_ID}")
    assert read.status_code == 404, read.text
    assert read.json()["error"] == "connection not found"

    delete = await api.request_raw("DELETE", f"/api/connectors/connections/{_MALFORMED_ID}")
    assert delete.status_code == 404, delete.text
    assert delete.json()["error"] == "connection not found"


async def test_start_connect_validation_body_carries_field_paths_not_values(connectors_stack: TaiStack) -> None:
    api = connectors_stack.api(port=connectors_stack.port_a)
    # A rejected config value shaped like a secret; it must NOT appear anywhere in the body.
    secret = "SENTINEL-SECRET-VALUE-9f3ab2"

    resp = await api.request_raw(
        "POST",
        "/api/connectors/connections/start",
        # ``config_values`` is dict[str, str]; a list value fails validation on that field.
        json={
            "provider_id": "e2e_idp",
            "alias": "x",
            "enabled_sub_services": ["default"],
            "config_values": {"client_secret": [secret]},
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"] == "invalid request body", body
    fields = body["fields"]
    assert isinstance(fields, list), body
    assert fields, body
    # Every entry names WHICH field failed and HOW — a dotted loc + a pydantic error type,
    # nothing else (no ``input``/``ctx`` that could carry the value).
    assert all(set(entry) == {"loc", "type"} for entry in fields), fields
    assert any("config_values" in entry["loc"] and "client_secret" in entry["loc"] for entry in fields), fields
    # N4: the submitted value is nowhere in the response.
    assert secret not in resp.text, resp.text
