"""Reacting-form key provisioning: register the derived public key, and loud failures when settings are missing."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.settings import reset_all_settings

from tai42_channel_whatsapp.flow_crypto import public_key_pem
from tai42_channel_whatsapp.provisioning import flow_encryption_material, provision_flow_encryption

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
    assert call["json"]["business_public_key"] == public_key_pem(_KEY)


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


def test_material_derives_the_public_key_for_the_default_number(provision_env, fake_httpx: FakeHttpx):
    material = flow_encryption_material()

    assert material.phone_number_id == PHONE_NUMBER_ID
    assert material.public_key_pem == public_key_pem(_KEY)
    assert fake_httpx.calls == []


def test_material_targets_an_explicit_number(provision_env, fake_httpx: FakeHttpx):
    material = flow_encryption_material("99999")

    assert material.phone_number_id == "99999"
    assert material.public_key_pem == public_key_pem(_KEY)
    assert fake_httpx.calls == []


def test_material_without_private_key_raises(monkeypatch: pytest.MonkeyPatch, fake_httpx: FakeHttpx):
    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID", PHONE_NUMBER_ID)
    reset_all_settings()
    try:
        with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY"):
            flow_encryption_material()
        assert fake_httpx.calls == []
    finally:
        reset_all_settings()


def test_material_without_a_sending_number_raises(monkeypatch: pytest.MonkeyPatch, fake_httpx: FakeHttpx):
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", _PRIVATE_PEM)
    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID", raising=False)
    reset_all_settings()
    try:
        with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"):
            flow_encryption_material()
        assert fake_httpx.calls == []
    finally:
        reset_all_settings()
