"""Binary/media surface: load_bytes / upload_bytes / stat, plus the size caps.

The two data-integrity invariants live here: reads use the raw endpoint (no size
cap, no >1 MiB silent-empty Contents-API trap) and uploads are capped loudly.
"""

from __future__ import annotations

import base64

import pytest
from httpx import HTTPStatusError

from tai42_storage_github import GithubStorage
from tai42_storage_github.storage import MAX_UPLOAD_BYTES
from tests.conftest import make_response

pytestmark = pytest.mark.usefixtures("client")


async def test_binary_round_trip_is_byte_identical(client):
    # Every byte value, including bytes that are not valid UTF-8.
    data = bytes(range(256)) * 8

    client.get.return_value = make_response(status=404)  # a create
    client.put.return_value = make_response(status=200)
    await GithubStorage().upload_bytes("media/blob.bin", data)

    encoded = client.put.call_args.kwargs["json"]["content"]
    assert base64.b64decode(encoded) == data

    # Read it back through the raw endpoint.
    client.get.return_value = make_response(status=200, content=base64.b64decode(encoded))
    loaded = await GithubStorage().load_bytes("media/blob.bin")

    assert loaded == data
    assert client.get.call_args.args[0].startswith("https://raw.githubusercontent.com/")


async def test_load_bytes_large_file_succeeds_via_raw(client):
    # A 2 MiB file — the Contents API would return content:"" for this; the raw
    # endpoint serves it in full.
    big = b"\x00\xff" * (1024 * 1024)  # 2 MiB
    client.get.return_value = make_response(status=200, content=big)

    loaded = await GithubStorage().load_bytes("big.bin")

    assert len(loaded) == len(big)
    assert loaded == big
    # Reads never touch the Contents API (that is the >1 MiB silent-empty trap).
    for call in client.get.call_args_list:
        assert "api.github.com" not in call.args[0]


async def test_load_bytes_missing_raises_filenotfound(client):
    client.get.return_value = make_response(status=404)

    with pytest.raises(FileNotFoundError):
        await GithubStorage().load_bytes("missing.bin")


async def test_upload_bytes_over_cap_raises_before_request(client):
    oversize = b"\x00" * (MAX_UPLOAD_BYTES + 1)

    with pytest.raises(ValueError, match="upload cap"):
        await GithubStorage().upload_bytes("big.bin", oversize)

    # The cap raises BEFORE any network call — no partial/truncated upload.
    client.get.assert_not_called()
    client.put.assert_not_called()


async def test_upload_bytes_at_cap_is_allowed(client):
    at_cap = b"\x00" * MAX_UPLOAD_BYTES
    client.get.return_value = make_response(status=404)
    client.put.return_value = make_response(status=200)

    await GithubStorage().upload_bytes("edge.bin", at_cap)

    client.put.assert_awaited_once()


async def test_upload_bytes_422_backstop_raises(client):
    client.get.return_value = make_response(status=404)
    client.put.return_value = make_response(status=422)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().upload_bytes("x.bin", b"within-cap")


async def test_stat_infers_mime_from_path_suffix():
    assert (await GithubStorage().stat("a/b/logo.png")).content_type == "image/png"
    assert (await GithubStorage().stat("clip.mp3")).content_type == "audio/mpeg"


async def test_stat_unknown_suffix_is_none():
    assert (await GithubStorage().stat("mystery.unknownext")).content_type is None
