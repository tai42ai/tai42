"""Tests for the shared Graph client module: the credential accessors (missing/empty raise), the
settings default, and every operation gating on credentials before any network work.

The ``_http_request`` curl seam itself is exercised end-to-end by the per-tool tests, which drive it
against a loopback stub under the ``curl_app`` fixture."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_whatsapp._internal.tools.whatsapp_client import (
    _access_token,
    _waba_id,
    delete_template,
    list_templates,
    register_template,
    subscribe_app,
    whatsapp_settings,
)


@pytest.mark.parametrize("access_token", [None, ""])
def test_missing_or_empty_access_token_raises(whatsapp_env: Callable[..., None], access_token: str | None) -> None:
    whatsapp_env(access_token=access_token)
    with pytest.raises(ValueError, match="CHANNEL_WHATSAPP_ACCESS_TOKEN"):
        _access_token()


@pytest.mark.parametrize("waba_id", [None, ""])
def test_missing_or_empty_waba_id_raises(whatsapp_env: Callable[..., None], waba_id: str | None) -> None:
    whatsapp_env(waba_id=waba_id)
    with pytest.raises(ValueError, match="CHANNEL_WHATSAPP_WABA_ID"):
        _waba_id()


def test_default_api_base_url(whatsapp_env: Callable[..., None]) -> None:
    whatsapp_env()
    assert whatsapp_settings().api_base_url == "https://graph.facebook.com/v23.0"


@pytest.mark.parametrize(
    "operation",
    [
        lambda: register_template("n", "en_US", "UTILITY", [{"type": "BODY", "text": "x"}]),
        list_templates,
        lambda: delete_template("n"),
        lambda: subscribe_app(None, None),
    ],
)
def test_every_operation_gates_on_credentials(whatsapp_env: Callable[..., None], operation: Callable[[], Any]) -> None:
    whatsapp_env(access_token=None)
    with pytest.raises(ValueError, match="CHANNEL_WHATSAPP_ACCESS_TOKEN"):
        asyncio.run(operation())
