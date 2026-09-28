"""Tests for the generic inbound-media wire vocabulary, pure builder, and placeholder helper."""

from __future__ import annotations

import pytest

from tai42_contract.conversations import (
    ENTRY_PARAM_VALUE_MAX_CHARS,
    MEDIA_FILENAME_PARAM,
    MEDIA_ID_PARAM,
    MEDIA_KIND_PARAM,
    MEDIA_MIME_TYPE_PARAM,
    MEDIA_SHA256_PARAM,
    MEDIA_SIZE_PARAM,
    MEDIA_VOICE_PARAM,
    STICKER_ANIMATED_PARAM,
    InboundMediaKind,
    InboundRejectionReason,
    build_inbound_media_params,
    inbound_media_placeholder,
)


def test_build_media_params_full_descriptor():
    params = build_inbound_media_params(
        kind=InboundMediaKind.IMAGE,
        media_id="abc",
        mime_type="image/png",
        filename="pic.png",
        sha256="deadbeef",
        voice=True,
        animated=True,
        size=1024,
    )
    assert params[MEDIA_KIND_PARAM] == "image"
    assert params[MEDIA_ID_PARAM] == "abc"
    assert params[MEDIA_MIME_TYPE_PARAM] == "image/png"
    assert params[MEDIA_FILENAME_PARAM] == "pic.png"
    assert params[MEDIA_SHA256_PARAM] == "deadbeef"
    assert params[MEDIA_SIZE_PARAM] == str(1024)
    assert params[MEDIA_VOICE_PARAM] == "true"
    assert params[STICKER_ANIMATED_PARAM] == "true"


def test_build_media_params_omits_absent_values():
    params = build_inbound_media_params(
        kind="document",
        media_id=None,
        mime_type="",
        filename=None,
        sha256=None,
        voice=False,
        animated=False,
        size=None,
    )
    assert params == {MEDIA_KIND_PARAM: "document"}
    for key in (
        MEDIA_ID_PARAM,
        MEDIA_MIME_TYPE_PARAM,
        MEDIA_FILENAME_PARAM,
        MEDIA_SHA256_PARAM,
        MEDIA_SIZE_PARAM,
        MEDIA_VOICE_PARAM,
        STICKER_ANIMATED_PARAM,
    ):
        assert key not in params


def test_build_media_params_drops_over_cap_value_not_truncated():
    at_cap = "a" * ENTRY_PARAM_VALUE_MAX_CHARS
    over_cap = "b" * (ENTRY_PARAM_VALUE_MAX_CHARS + 1)
    at = build_inbound_media_params(kind="image", media_id=at_cap)
    assert at[MEDIA_ID_PARAM] == at_cap
    over = build_inbound_media_params(kind="image", media_id=over_cap)
    assert MEDIA_ID_PARAM not in over


def test_build_media_params_unknown_kind_raises():
    with pytest.raises(ValueError, match="gif"):
        build_inbound_media_params(kind="gif")


def test_placeholder_per_kind():
    assert inbound_media_placeholder(InboundMediaKind.IMAGE) == "[image]"
    assert inbound_media_placeholder(InboundMediaKind.DOCUMENT) == "[document]"
    assert inbound_media_placeholder(InboundMediaKind.AUDIO) == "[audio]"
    assert inbound_media_placeholder(InboundMediaKind.VIDEO) == "[video]"
    assert inbound_media_placeholder(InboundMediaKind.STICKER) == "[sticker]"
    assert inbound_media_placeholder(InboundMediaKind.FILE) == "[file]"


def test_placeholder_document_and_file_with_filename():
    assert inbound_media_placeholder("document", filename="r.pdf") == "[document: r.pdf]"
    assert inbound_media_placeholder("file", filename="r.pdf") == "[file: r.pdf]"
    assert inbound_media_placeholder("document", filename="   ") == "[document]"


def test_placeholder_voice():
    assert inbound_media_placeholder("audio", voice=True) == "[voice message]"
    assert inbound_media_placeholder("audio", voice=False) == "[audio]"
    assert inbound_media_placeholder("image", voice=True) == "[image]"


def test_placeholder_unknown_kind_raises():
    with pytest.raises(ValueError, match="gif"):
        inbound_media_placeholder("gif")


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
        "build_inbound_media_params",
        "inbound_media_placeholder",
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
