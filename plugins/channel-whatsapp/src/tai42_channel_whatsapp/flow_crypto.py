"""WhatsApp Flow data-endpoint encryption.

A reacting Flow's data endpoint exchanges AES-128-GCM encrypted payloads with Meta. Each
request carries three base64 fields: ``encrypted_aes_key`` (the per-exchange AES key encrypted
to the business PUBLIC key with RSA-2048 OAEP-SHA256), ``initial_vector`` (the AES-GCM IV), and
``encrypted_flow_data`` (the request JSON, AES-128-GCM encrypted with the 16-byte auth tag
appended). The endpoint:

* decrypts the AES key with the business PRIVATE key (``RSA/ECB/OAEPWithSHA-256AndMGF1Padding``);
* decrypts the flow data with AES-128-GCM under that key and IV (the trailing 16 bytes are the tag);
* re-encrypts its response with the SAME AES key and the IV with every bit flipped (XOR ``0xFF``),
  returning base64 of ``ciphertext + tag``.

Nothing here touches the platform or the react facet — it is pure transport crypto. A malformed
or undecryptable payload raises :class:`FlowPayloadDecryptError` (the route maps it to HTTP 421).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from cryptography.exceptions import InvalidKey, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Meta appends a 16-byte GCM authentication tag to the ciphertext.
_GCM_TAG_BYTES = 16
# The AES key Meta sends is 128-bit.
_AES_KEY_BYTES = 16


class FlowPayloadDecryptError(Exception):
    """A flow data-endpoint request could not be decrypted (mapped to HTTP 421)."""


def load_private_key(pem: str, passphrase: str | None) -> rsa.RSAPrivateKey:
    """Load the business RSA private key from its PEM text.

    Raises ``ValueError`` when the PEM is unreadable or not an RSA private key, so a
    misconfigured key fails loudly at use rather than silently.
    """
    try:
        key = serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=passphrase.encode("utf-8") if passphrase else None,
        )
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ValueError("WhatsApp flow private key is not a readable PEM private key") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("WhatsApp flow private key must be an RSA private key")  # noqa: TRY004 a misconfigured key is a config ValueError, not a caller type error
    return key


def public_key_pem(private_key: rsa.RSAPrivateKey) -> str:
    """The PEM of the public key matching ``private_key`` — the key registered for the sending number."""
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


def _b64decode(value: Any, field: str) -> bytes:
    if not isinstance(value, str):
        raise FlowPayloadDecryptError(f"flow endpoint request field {field!r} must be a base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FlowPayloadDecryptError(f"flow endpoint request field {field!r} is not valid base64") from exc


def decrypt_request(body: dict[str, Any], private_key: rsa.RSAPrivateKey) -> tuple[dict[str, Any], bytes, bytes]:
    """Decrypt an endpoint request body; return ``(request_json, aes_key, iv)``.

    ``aes_key`` + ``iv`` are returned so the response can be re-encrypted under the same key
    with the flipped IV. Raises :class:`FlowPayloadDecryptError` on any malformed field, a key
    that cannot unwrap the AES key, a failed GCM tag check, or a plaintext that is not a JSON object.
    """
    import json

    encrypted_aes_key = _b64decode(body.get("encrypted_aes_key"), "encrypted_aes_key")
    iv = _b64decode(body.get("initial_vector"), "initial_vector")
    encrypted_flow_data = _b64decode(body.get("encrypted_flow_data"), "encrypted_flow_data")
    try:
        aes_key = private_key.decrypt(
            encrypted_aes_key,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
    except (ValueError, InvalidKey) as exc:
        raise FlowPayloadDecryptError("flow endpoint request AES key could not be decrypted") from exc
    if len(aes_key) != _AES_KEY_BYTES:
        raise FlowPayloadDecryptError("flow endpoint request AES key is not 128-bit")
    if len(encrypted_flow_data) < _GCM_TAG_BYTES:
        raise FlowPayloadDecryptError("flow endpoint request ciphertext is too short to carry a GCM tag")
    try:
        plaintext = AESGCM(aes_key).decrypt(iv, encrypted_flow_data, None)
    except Exception as exc:
        raise FlowPayloadDecryptError("flow endpoint request payload failed AES-GCM decryption") from exc
    try:
        request = json.loads(plaintext)
    except ValueError as exc:
        raise FlowPayloadDecryptError("flow endpoint request payload is not valid JSON") from exc
    if not isinstance(request, dict):
        raise FlowPayloadDecryptError("flow endpoint request payload is not a JSON object")
    return request, aes_key, iv


def encrypt_response(response: dict[str, Any], aes_key: bytes, iv: bytes) -> str:
    """Encrypt an endpoint response under ``aes_key`` with the bit-flipped ``iv``; return base64(ciphertext+tag)."""
    import json

    flipped_iv = bytes(byte ^ 0xFF for byte in iv)
    plaintext = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = AESGCM(aes_key).encrypt(flipped_iv, plaintext, None)
    return base64.b64encode(ciphertext).decode("ascii")
