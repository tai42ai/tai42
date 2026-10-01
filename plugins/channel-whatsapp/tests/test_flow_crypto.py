"""The Flow data-endpoint crypto: RSA-unwrap + AES-128-GCM round-trip, IV flip, and failures."""

from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tai42_channel_whatsapp.flow_crypto import (
    FlowPayloadDecryptError,
    decrypt_request,
    encrypt_response,
    load_private_key,
    public_key_pem,
)


def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _encrypt_request(private_key: rsa.RSAPrivateKey, payload: dict) -> tuple[dict, bytes, bytes]:
    """Encrypt a request exactly as Meta would, returning the envelope + the (aes_key, iv) used."""
    aes_key = os.urandom(16)
    iv = os.urandom(16)
    encrypted_aes_key = private_key.public_key().encrypt(
        aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    ciphertext = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode("utf-8"), None)
    envelope = {
        "encrypted_aes_key": base64.b64encode(encrypted_aes_key).decode(),
        "initial_vector": base64.b64encode(iv).decode(),
        "encrypted_flow_data": base64.b64encode(ciphertext).decode(),
    }
    return envelope, aes_key, iv


def test_decrypt_request_round_trips_and_returns_key_and_iv():
    key = _keypair()
    payload = {"version": "3.0", "action": "data_exchange", "flow_token": "int-9", "data": {"a": "b"}}
    envelope, aes_key, iv = _encrypt_request(key, payload)

    request, out_key, out_iv = decrypt_request(envelope, key)

    assert request == payload
    assert out_key == aes_key
    assert out_iv == iv


def test_encrypt_response_uses_the_flipped_iv_and_is_decodable_by_meta():
    key = _keypair()
    _, aes_key, iv = _encrypt_request(key, {"action": "ping"})

    body = encrypt_response({"data": {"status": "active"}}, aes_key, iv)

    # Meta decrypts the response with the SAME AES key and the bit-flipped IV.
    flipped = bytes(b ^ 0xFF for b in iv)
    plaintext = AESGCM(aes_key).decrypt(flipped, base64.b64decode(body), None)
    assert json.loads(plaintext) == {"data": {"status": "active"}}


def test_decrypt_rejects_non_base64_field():
    key = _keypair()
    envelope, _, _ = _encrypt_request(key, {"action": "ping"})
    envelope["initial_vector"] = "not base64!!"
    with pytest.raises(FlowPayloadDecryptError, match="initial_vector"):
        decrypt_request(envelope, key)


def test_decrypt_rejects_missing_field():
    key = _keypair()
    with pytest.raises(FlowPayloadDecryptError, match="encrypted_aes_key"):
        decrypt_request({"initial_vector": "AAAA", "encrypted_flow_data": "AAAA"}, key)


def test_decrypt_rejects_tampered_ciphertext():
    key = _keypair()
    envelope, _, _ = _encrypt_request(key, {"action": "ping"})
    raw = bytearray(base64.b64decode(envelope["encrypted_flow_data"]))
    raw[0] ^= 0x01
    envelope["encrypted_flow_data"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(FlowPayloadDecryptError, match="AES-GCM"):
        decrypt_request(envelope, key)


def test_decrypt_rejects_aes_key_from_a_different_key():
    key = _keypair()
    other = _keypair()
    envelope, _, _ = _encrypt_request(other, {"action": "ping"})
    with pytest.raises(FlowPayloadDecryptError, match="AES key"):
        decrypt_request(envelope, key)


def test_decrypt_rejects_non_object_plaintext():
    key = _keypair()
    envelope, _, _ = _encrypt_request(key, {"action": "ping"})
    # Re-encrypt a JSON array (valid JSON, not an object).
    aes_key = os.urandom(16)
    iv = os.urandom(16)
    envelope["encrypted_aes_key"] = base64.b64encode(
        key.public_key().encrypt(
            aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        )
    ).decode()
    envelope["initial_vector"] = base64.b64encode(iv).decode()
    envelope["encrypted_flow_data"] = base64.b64encode(AESGCM(aes_key).encrypt(iv, b"[1,2,3]", None)).decode()
    with pytest.raises(FlowPayloadDecryptError, match="not a JSON object"):
        decrypt_request(envelope, key)


def test_load_private_key_round_trips_pem_and_public_key():
    key = _keypair()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    loaded = load_private_key(pem, None)
    assert isinstance(loaded, rsa.RSAPrivateKey)
    assert "PUBLIC KEY" in public_key_pem(loaded)


def test_load_private_key_with_passphrase():
    key = _keypair()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(b"secret"),
    ).decode()

    assert isinstance(load_private_key(pem, "secret"), rsa.RSAPrivateKey)


def test_load_private_key_rejects_garbage():
    with pytest.raises(ValueError, match="readable PEM"):
        load_private_key("not a pem", None)


def test_load_private_key_rejects_non_rsa():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    pem = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError, match="RSA private key"):
        load_private_key(pem, None)


def test_decrypt_rejects_non_json_plaintext():
    key = _keypair()
    aes_key = os.urandom(16)
    iv = os.urandom(16)
    encrypted_aes_key = key.public_key().encrypt(
        aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    envelope = {
        "encrypted_aes_key": base64.b64encode(encrypted_aes_key).decode(),
        "initial_vector": base64.b64encode(iv).decode(),
        "encrypted_flow_data": base64.b64encode(AESGCM(aes_key).encrypt(iv, b"not json", None)).decode(),
    }
    with pytest.raises(FlowPayloadDecryptError, match="not valid JSON"):
        decrypt_request(envelope, key)
