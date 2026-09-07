"""AES-GCM-256 wrap/unwrap for connector token blobs.

Blob layout: [1-byte format version] || [12-byte nonce] || [ciphertext + 16-byte
GCM tag]. The leading version byte (``0x01``) names the blob format version, so a
reader detects a blob written in a different format instead of misreading it.
Encryption always uses the current ``CONNECTORS_KEK``; decryption trial-decrypts a
key ring — the current key followed by any ``CONNECTORS_KEK_PREVIOUS`` keys — so a KEK
rotation can serve blobs still under a superseded key until the re-encrypt sweep
converges. The connection_id is bound as AAD so a blob cannot be swapped between
connections.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from tai42_contract.connectors.errors import ConnectorError

from tai42_skeleton.connectors.settings import connector_crypto_secrets

_KEK_FORMAT_VERSION = 0x01
_NONCE_LEN = 12
_TAG_LEN = 16


class ConnectorEncryptionConfigError(ConnectorError):
    """Raised when CONNECTORS_KEK is missing or malformed at use time."""


def ensure_kek() -> bytes:
    """Return the current KEK used to encrypt blobs, or raise a config error."""
    try:
        return connector_crypto_secrets().require_kek_bytes()
    except (RuntimeError, ValueError) as exc:
        raise ConnectorEncryptionConfigError(str(exc)) from exc


def ensure_decrypt_ring() -> list[bytes]:
    """Return the decrypt key ring — the current KEK first, then any previous KEKs —
    or raise a config error when the current KEK is missing/malformed."""
    try:
        return connector_crypto_secrets().require_decrypt_ring_bytes()
    except (RuntimeError, ValueError) as exc:
        raise ConnectorEncryptionConfigError(str(exc)) from exc


def _aad(connection_id: str) -> bytes:
    return connection_id.encode("ascii")


def encrypt(plaintext: bytes, *, connection_id: str) -> bytes:
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes")
    kek = ensure_kek()
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(kek).encrypt(nonce, bytes(plaintext), _aad(connection_id))
    return bytes([_KEK_FORMAT_VERSION]) + nonce + ct


def _decrypt_with_ring(blob: bytes, *, connection_id: str, ring: list[bytes]) -> tuple[bytes, int]:
    """Trial-decrypt ``blob`` against ``ring`` (current key first, then each previous
    key), returning ``(plaintext, ring_index)`` for the first key that opens it.

    A blob no ring key can open is unreadable: the ``InvalidTag`` propagates loudly
    rather than returning a silently-undecrypted blob. The shape/version guards run
    before any key is tried, so a wrong format fails as a ``ValueError`` (not a tag
    mismatch)."""
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError("blob must be bytes")
    blob = bytes(blob)
    if len(blob) < 1 + _NONCE_LEN + _TAG_LEN:
        raise ValueError("blob too short to contain version+nonce+tag")
    version = blob[0]
    if version != _KEK_FORMAT_VERSION:
        raise ValueError(f"unsupported connector token-blob format version byte: {version:#04x}")
    nonce, ct = blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
    aad = _aad(connection_id)
    for index, key in enumerate(ring):
        try:
            return AESGCM(key).decrypt(nonce, ct, aad), index
        except InvalidTag:
            continue
    raise InvalidTag


def decrypt(blob: bytes, *, connection_id: str) -> bytes:
    plaintext, _ = _decrypt_with_ring(blob, connection_id=connection_id, ring=ensure_decrypt_ring())
    return plaintext


def decrypt_reporting_key(blob: bytes, *, connection_id: str) -> tuple[bytes, bool]:
    """Decrypt via the ring, returning ``(plaintext, under_current_key)``.

    Used by the re-encrypt sweep to skip a blob already under the current key: the
    current key is ring index 0, so ``under_current_key`` is ``True`` only when the
    current key opened the blob."""
    plaintext, index = _decrypt_with_ring(blob, connection_id=connection_id, ring=ensure_decrypt_ring())
    return plaintext, index == 0
