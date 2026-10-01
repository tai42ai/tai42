"""Reacting-form key provisioning: register the derived public key, and loud failures when settings are missing."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.settings import reset_all_settings

from tai42_channel_whatsapp.provisioning import provision_flow_encryption

from .conftest import PHONE_NUMBER_ID, FakeHttpx, response

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


@pytest.fixture
def provision_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CHANNEL_WHATSAPP_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID", PHONE_NUMBER_ID)
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", _PRIVATE_PEM)
    reset_all_settings()
    yield
    reset_all_settings()


async def test_provision_registers_the_derived_public_key(provision_env, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(200, json={"success": True}))

    await provision_flow_encryption()

    call = fake_httpx.calls[0]
    assert call["url"] == f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/whatsapp_business_encryption"
    assert "PUBLIC KEY" in call["json"]["business_public_key"]


async def test_provision_targets_an_explicit_number(provision_env, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(200, json={"success": True}))

    await provision_flow_encryption("99999")

    assert fake_httpx.calls[0]["url"].endswith("/99999/whatsapp_business_encryption")


async def test_provision_without_private_key_raises(monkeypatch: pytest.MonkeyPatch, fake_httpx: FakeHttpx):
    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID", PHONE_NUMBER_ID)
    reset_all_settings()
    try:
        with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY"):
            await provision_flow_encryption()
        assert fake_httpx.calls == []
    finally:
        reset_all_settings()


async def test_provision_without_a_sending_number_raises(monkeypatch: pytest.MonkeyPatch, fake_httpx: FakeHttpx):
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", _PRIVATE_PEM)
    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID", raising=False)
    reset_all_settings()
    try:
        with pytest.raises(ChannelDeliveryError, match="DEFAULT_PHONE_NUMBER_ID"):
            await provision_flow_encryption()
    finally:
        reset_all_settings()
