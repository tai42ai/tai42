"""AES-GCM token-blob crypto: round-trip, AAD binding, tamper + config errors,
and the format-version byte."""

from __future__ import annotations

import base64
from typing import cast

import pytest
from cryptography.exceptions import InvalidTag
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.oauth.crypto import ConnectorEncryptionConfigError

from .conftest import CID, CID2, TEST_KEK_B64


def test_encrypt_decrypt_round_trip():
    plaintext = b"\x00\x01secret-token-payload\xff"
    blob = crypto.encrypt(plaintext, connection_id=CID)
    assert blob != plaintext
    assert crypto.decrypt(blob, connection_id=CID) == plaintext


def test_blob_carries_format_version_byte():
    # Every blob leads with 0x01 so a future format can be told apart on read.
    blob = crypto.encrypt(b"payload", connection_id=CID)
    assert blob[0] == 0x01


def test_nonce_makes_ciphertext_unique_per_call():
    a = crypto.encrypt(b"same", connection_id=CID)
    b = crypto.encrypt(b"same", connection_id=CID)
    assert a != b  # fresh 12-byte nonce each time
    assert crypto.decrypt(a, connection_id=CID) == b"same"
    assert crypto.decrypt(b, connection_id=CID) == b"same"


def test_connection_id_is_bound_as_aad():
    """A blob encrypted for one connection cannot be decrypted under another."""
    blob = crypto.encrypt(b"payload", connection_id=CID)
    with pytest.raises(InvalidTag):
        crypto.decrypt(blob, connection_id=CID2)


def test_tampered_ciphertext_raises():
    blob = bytearray(crypto.encrypt(b"payload", connection_id=CID))
    blob[-1] ^= 0x01  # flip a tag bit
    with pytest.raises(InvalidTag):
        crypto.decrypt(bytes(blob), connection_id=CID)


def test_encrypt_rejects_non_bytes():
    # Deliberately feeds a wrong runtime type (str) to exercise the TypeError guard.
    with pytest.raises(TypeError):
        crypto.encrypt(cast(bytes, "not-bytes"), connection_id=CID)


def test_decrypt_rejects_non_bytes():
    # Deliberately feeds a wrong runtime type (str) to exercise the TypeError guard.
    with pytest.raises(TypeError):
        crypto.decrypt(cast(bytes, "not-bytes"), connection_id=CID)


def test_decrypt_rejects_short_blob():
    with pytest.raises(ValueError, match="too short"):
        crypto.decrypt(b"short", connection_id=CID)


def test_decrypt_rejects_unknown_version_byte():
    # A blob whose leading version byte is not 0x01 is an unreadable format, not a
    # tag mismatch — it fails loudly before any key is tried.
    blob = bytearray(crypto.encrypt(b"payload", connection_id=CID))
    blob[0] = 0x02
    with pytest.raises(ValueError, match="version"):
        crypto.decrypt(bytes(blob), connection_id=CID)


def test_decrypt_uses_a_previous_key_from_the_ring(monkeypatch):
    # A blob written under key A still decrypts after CONNECTORS_KEK rotates to B,
    # because A is retained in CONNECTORS_KEK_PREVIOUS — the decrypt ring tries it.
    blob = crypto.encrypt(b"payload", connection_id=CID)  # under A (crypto_env default)
    key_b = base64.b64encode(bytes(range(96, 128))).decode("ascii")
    monkeypatch.setenv("CONNECTORS_KEK", key_b)
    monkeypatch.setenv("CONNECTORS_KEK_PREVIOUS", TEST_KEK_B64)
    reset_all_settings()
    assert crypto.decrypt(blob, connection_id=CID) == b"payload"


def test_decrypt_fails_when_no_ring_key_matches(monkeypatch):
    # Once the old key is dropped from the ring (rotation converged, previous retired),
    # a blob still under it is dead — surfacing as InvalidTag, never silently readable.
    blob = crypto.encrypt(b"payload", connection_id=CID)  # under A
    key_b = base64.b64encode(bytes(range(96, 128))).decode("ascii")
    monkeypatch.setenv("CONNECTORS_KEK", key_b)
    monkeypatch.delenv("CONNECTORS_KEK_PREVIOUS", raising=False)
    reset_all_settings()
    with pytest.raises(InvalidTag):
        crypto.decrypt(blob, connection_id=CID)


def test_decrypt_reporting_key_flags_current_vs_previous(monkeypatch):
    # The sweep helper reports whether the CURRENT key opened the blob: True under the
    # current key, False once the blob is only openable by a retained previous key.
    blob = crypto.encrypt(b"payload", connection_id=CID)  # under A (current)
    plaintext, under_current = crypto.decrypt_reporting_key(blob, connection_id=CID)
    assert plaintext == b"payload"
    assert under_current is True

    key_b = base64.b64encode(bytes(range(96, 128))).decode("ascii")
    monkeypatch.setenv("CONNECTORS_KEK", key_b)
    monkeypatch.setenv("CONNECTORS_KEK_PREVIOUS", TEST_KEK_B64)
    reset_all_settings()
    plaintext, under_current = crypto.decrypt_reporting_key(blob, connection_id=CID)
    assert plaintext == b"payload"
    assert under_current is False


def test_encrypt_accepts_bytearray():
    # encrypt accepts bytearray at runtime (isinstance bytes|bytearray) though the
    # param is annotated bytes; cast to exercise the bytearray path without altering it.
    blob = crypto.encrypt(cast(bytes, bytearray(b"abc")), connection_id=CID)
    assert crypto.decrypt(blob, connection_id=CID) == b"abc"


def test_missing_kek_raises_config_error(monkeypatch):
    monkeypatch.delenv("CONNECTORS_KEK", raising=False)
    reset_all_settings()
    with pytest.raises(ConnectorEncryptionConfigError):
        crypto.ensure_kek()


def test_missing_kek_raises_config_error_via_decrypt(monkeypatch):
    # With no CONNECTORS_KEK configured, decrypt fails as a loud config error rather
    # than a tag mismatch — the missing key surfaces through decrypt itself, not only
    # through a direct ensure_kek() call.
    blob = crypto.encrypt(b"payload", connection_id=CID)
    monkeypatch.delenv("CONNECTORS_KEK", raising=False)
    reset_all_settings()
    with pytest.raises(ConnectorEncryptionConfigError):
        crypto.decrypt(blob, connection_id=CID)


def test_malformed_kek_raises_config_error_via_settings(monkeypatch):
    # A non-base64 KEK is rejected by the settings validator; ensure_kek wraps it.
    monkeypatch.setenv("CONNECTORS_KEK", "!!!not-base64!!!")
    reset_all_settings()
    with pytest.raises(ConnectorEncryptionConfigError):
        crypto.ensure_kek()
