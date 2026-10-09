"""Tests for the generic inbound-media wire vocabulary."""

from __future__ import annotations

from tai42_contract.conversations import InboundMediaKind, InboundRejectionReason


def test_inbound_media_kind_values():
    assert [kind.value for kind in InboundMediaKind] == ["image", "document", "audio", "video", "sticker", "file"]


def test_inbound_media_symbols_reexported_from_conversations():
    import tai42_contract.conversations as conv

    names = [
        "InboundMediaKind",
        "MEDIA_KIND_PARAM",
        "MEDIA_ID_PARAM",
        "MEDIA_MIME_TYPE_PARAM",
        "MEDIA_FILENAME_PARAM",
        "MEDIA_SHA256_PARAM",
        "MEDIA_VOICE_PARAM",
        "STICKER_ANIMATED_PARAM",
        "MEDIA_SIZE_PARAM",
    ]
    for name in names:
        assert hasattr(conv, name)
        assert name in conv.__all__


def test_inbound_rejection_reason_values():
    assert InboundRejectionReason.UNSUPPORTED_TYPE == "unsupported_type"
    assert InboundRejectionReason.TOO_LARGE == "too_large"
    assert InboundRejectionReason.COULD_NOT_RECEIVE == "could_not_receive"

    import tai42_contract.conversations as conv

    assert conv.InboundRejectionReason is InboundRejectionReason
    assert "InboundRejectionReason" in conv.__all__
