"""``ImageReference``: an addressable image (a public URL or a base64 data URI) with its asserted mime."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.template import ImageReference


def test_image_reference_carries_url_and_mime():
    ref = ImageReference(url="data:image/png;base64,AAAA", mime="image/png")
    assert ref.url == "data:image/png;base64,AAAA"
    assert ref.mime == "image/png"


def test_image_reference_mime_may_be_unknown_for_a_public_url():
    assert ImageReference(url="https://example.com/img", mime=None).mime is None


def test_image_reference_is_frozen():
    ref = ImageReference(url="https://example.com/a.png", mime="image/png")
    with pytest.raises(ValidationError):
        ref.url = "https://example.com/b.png"  # type: ignore[misc]
