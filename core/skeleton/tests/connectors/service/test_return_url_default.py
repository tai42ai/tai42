"""An omitted ``return_url`` lands on the declared connectors page, on every flow that starts consent."""

from __future__ import annotations

import pytest
from tai42_contract.connectors.service import StartConnectResult

from tai42_skeleton.connectors.service.connection_service import patch_sub_services, start_connect, start_reconnect
from tai42_skeleton.connectors.service.connection_service.validation import _validate_return_url

from ..conftest import CID, make_oauth_record
from .conftest import ORIGIN, REDIRECT


@pytest.fixture(autouse=True)
def _declared_landing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTED_UI_CONNECTORS_PATH", "/landing")


def test_an_omitted_return_url_resolves_to_the_declared_connectors_path():
    assert _validate_return_url(None) == "/landing"
    assert _validate_return_url("/elsewhere") == "/elsewhere"


async def test_start_connect_without_return_url_lands_on_the_declared_page(harness):
    _, _, _, flows, _, _ = harness
    result = await start_connect(
        provider_id="acme",
        alias="work",
        enabled_sub_services=["mail"],
        return_url=None,
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert isinstance(result, StartConnectResult)
    assert flows[result.flow_id].return_url == "/landing"


async def test_start_reconnect_without_return_url_lands_on_the_declared_page(harness):
    _, records, _, flows, _, _ = harness
    records[CID] = make_oauth_record(alias="work")
    result = await start_reconnect(
        connection_id=CID,
        enabled_sub_services=["mail", "cal"],
        return_url=None,
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert flows[result.flow_id].return_url == "/landing"


async def test_patch_consent_without_return_url_lands_on_the_declared_page(harness):
    _, records, _, flows, _, _ = harness
    records[CID] = make_oauth_record(
        connection_id=CID, enabled_sub_services=["mail"], granted_scopes=["mail.read", "mail.send"]
    )
    result = await patch_sub_services(
        connection_id=CID, desired=["mail", "cal"], return_url=None, redirect_uri=REDIRECT, origin=ORIGIN
    )
    assert result.flow_id is not None
    assert flows[result.flow_id].return_url == "/landing"


def test_an_omitted_return_url_never_answers_with_a_return_url_refusal(monkeypatch: pytest.MonkeyPatch):
    # A declared connectors path the landing rule refuses is the setting's error, never a return_url one.
    monkeypatch.setenv("HOSTED_UI_CONNECTORS_PATH", "/connectors#mine")
    with pytest.raises(ValueError, match="HOSTED_UI_CONNECTORS_PATH") as caught:
        _validate_return_url(None)
    assert "return_url" not in str(caught.value)
