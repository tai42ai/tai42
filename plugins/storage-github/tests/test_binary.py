"""Binary/media surface: load_bytes / upload_bytes / stat / load_range, plus the size caps.

Whole reads and ``stat`` are one Contents API call at the branch (the raw form serves
every byte, the object form the size); a ranged read resolves the branch head and
reads the raw host at that commit; uploads are capped loudly.
"""

from __future__ import annotations

import base64

import pytest
from httpx import HTTPStatusError
from tai42_contract.storage import ObjectStat

from tai42_storage_github import GithubStorage
from tai42_storage_github.storage import MAX_UPLOAD_BYTES

from .conftest import make_response, set_stream

pytestmark = pytest.mark.usefixtures("client")


CONTENTS = "https://api.github.com/repos/acme/content/contents"
RAW_FORM = "application/vnd.github.raw+json"
RAW_FORM_HEADERS = {"Content-Type": f"{RAW_FORM}; charset=utf-8"}
OBJECT_FORM = "application/vnd.github.object+json"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
REF_URL = "https://api.github.com/repos/acme/content/git/ref/heads/dev"


def _ref_answer(sha: object = HEAD_SHA) -> object:
    return make_response(status=200, json_body={"ref": "refs/heads/dev", "object": {"type": "commit", "sha": sha}})


async def test_binary_round_trip_is_byte_identical(client):
    # Every byte value, including bytes that are not valid UTF-8.
    data = bytes(range(256)) * 8

    client.get.return_value = make_response(status=404)  # a create
    client.put.return_value = make_response(status=200)
    await GithubStorage().upload_bytes("media/blob.bin", data)

    encoded = client.put.call_args.kwargs["json"]["content"]
    assert base64.b64decode(encoded) == data

    client.get.reset_mock()
    client.get.return_value = make_response(status=200, content=base64.b64decode(encoded), headers=RAW_FORM_HEADERS)
    loaded = await GithubStorage().load_bytes("media/blob.bin")

    assert loaded == data
    client.get.assert_awaited_once()
    args, kwargs = client.get.call_args
    assert args[0] == f"{CONTENTS}/media/blob.bin"
    assert kwargs["params"] == {"ref": "dev"}
    assert kwargs["headers"]["Accept"] == RAW_FORM
    assert "Range" not in kwargs["headers"]
    client.stream.assert_not_called()


async def test_load_bytes_large_file_is_read_whole(client):
    big = b"\x00\xff" * (1024 * 1024)  # 2 MiB
    client.get.return_value = make_response(status=200, content=big, headers=RAW_FORM_HEADERS)

    loaded = await GithubStorage().load_bytes("big.bin")

    assert loaded == big
    for call in client.get.call_args_list:
        assert "raw.githubusercontent.com" not in call.args[0]


async def test_load_bytes_missing_raises_filenotfound(client):
    client.get.return_value = make_response(status=404)

    with pytest.raises(FileNotFoundError):
        await GithubStorage().load_bytes("missing.bin")


async def test_load_bytes_of_a_directory_listing_raises_filenotfound(client):
    client.get.return_value = make_response(
        status=200, content=b"[]", headers={"Content-Type": "application/json; charset=utf-8"}
    )

    with pytest.raises(FileNotFoundError, match="Object not found: media"):
        await GithubStorage().load_bytes("media")


async def test_load_bytes_unexpected_success_status_raises(client):
    client.get.return_value = make_response(status=204, headers=RAW_FORM_HEADERS)

    with pytest.raises(RuntimeError, match="204"):
        await GithubStorage().load_bytes("x.bin")


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


# --- stat ---------------------------------------------------------------------


async def test_stat_reads_the_object_form_size_at_the_branch(client):
    client.get.return_value = make_response(
        status=200, json_body={"type": "file", "size": 26214400, "path": "a/b/logo.png"}
    )

    assert await GithubStorage().stat("a/b/logo.png") == ObjectStat(content_type="image/png", size=26214400)
    client.get.assert_awaited_once()
    args, kwargs = client.get.call_args
    assert args[0] == f"{CONTENTS}/a/b/logo.png"
    assert kwargs["params"] == {"ref": "dev"}
    assert kwargs["headers"]["Accept"] == OBJECT_FORM
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert "Range" not in kwargs["headers"]
    client.stream.assert_not_called()


async def test_stat_unknown_suffix_is_none(client):
    client.get.return_value = make_response(status=200, json_body={"type": "file", "size": 3})

    assert await GithubStorage().stat("mystery.unknownext") == ObjectStat(content_type=None, size=3)


async def test_stat_missing_raises_filenotfound(client):
    client.get.return_value = make_response(status=404)

    with pytest.raises(FileNotFoundError, match=r"Object not found: gone\.png"):
        await GithubStorage().stat("gone.png")


@pytest.mark.parametrize(
    "answer",
    [[{"path": "d/a.j2", "type": "file"}], {"type": "dir", "entries": []}],
    ids=["default-form-list", "object-form-dir"],
)
async def test_stat_of_a_directory_raises_filenotfound(client, answer):
    client.get.return_value = make_response(status=200, json_body=answer)

    with pytest.raises(FileNotFoundError, match="Object not found: d"):
        await GithubStorage().stat("d")


@pytest.mark.parametrize("answer", [{"type": "file"}, {"type": "file", "size": "12"}, {"type": "file", "size": True}])
async def test_stat_of_a_file_without_an_integer_size_raises(client, answer):
    client.get.return_value = make_response(status=200, json_body=answer)

    with pytest.raises(RuntimeError, match=r"contents/x\.bin"):
        await GithubStorage().stat("x.bin")


async def test_stat_server_error_propagates(client):
    client.get.return_value = make_response(status=500)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().stat("x.bin")


# --- load_range -----------------------------------------------------------------


async def test_load_range_resolves_the_head_then_reads_the_raw_host_at_that_commit(client, settings):
    client.get.return_value = _ref_answer()
    resp = make_response(status=206, content=b"\x89PNG", headers={"Content-Range": "bytes 0-3/100"})
    set_stream(client, resp)

    assert await GithubStorage().load_range("p", 0, 4) == b"\x89PNG"

    assert [c[0] for c in client.mock_calls if c[0] in ("get", "stream")] == ["get", "stream"]
    ref_args, ref_kwargs = client.get.call_args
    assert ref_args[0] == REF_URL
    assert ref_kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert ref_kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert ref_kwargs["headers"]["Authorization"] == f"Bearer {settings.token.get_secret_value()}"
    (method, url), raw_kwargs = client.stream.call_args
    assert method == "GET"
    assert url == f"https://raw.githubusercontent.com/acme/content/{HEAD_SHA}/p"
    assert raw_kwargs["headers"] == {
        "Authorization": f"Bearer {settings.token.get_secret_value()}",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-3",
    }
    for call in [*client.get.call_args_list, *client.stream.call_args_list]:
        assert "refs/heads/dev" not in str(call)
        assert "/dev/" not in str(call)


async def test_load_range_of_the_tail_sends_the_tail_range(client):
    client.get.return_value = _ref_answer()
    set_stream(client, make_response(status=206, content=b"tail"))

    assert await GithubStorage().load_range("blob.bin", 96, 4) == b"tail"
    assert client.stream.call_args.kwargs["headers"]["Range"] == "bytes=96-99"


async def test_load_range_running_past_the_end_answers_the_tail_only(client):
    # The raw host clamps a range whose end is past the object to the bytes it has.
    client.get.return_value = _ref_answer()
    set_stream(client, make_response(status=206, content=b"ab", headers={"Content-Range": "bytes 98-99/100"}))

    assert await GithubStorage().load_range("blob.bin", 98, 10) == b"ab"
    assert client.stream.call_args.kwargs["headers"]["Range"] == "bytes=98-107"


async def test_load_range_past_the_end_answers_empty(client):
    client.get.return_value = _ref_answer()
    set_stream(client, make_response(status=416, headers={"Content-Range": "bytes */100"}))

    assert await GithubStorage().load_range("blob.bin", 100, 4) == b""


async def test_load_range_missing_or_directory_raises_filenotfound(client):
    client.get.return_value = _ref_answer()
    set_stream(client, make_response(status=404))

    with pytest.raises(FileNotFoundError, match="Object not found: jq"):
        await GithubStorage().load_range("jq", 0, 4)


async def test_load_range_whole_object_answer_raises_without_reading_the_body(client):
    client.get.return_value = _ref_answer()
    resp = make_response(status=200, content=b"x" * 1000)
    set_stream(client, resp)

    with pytest.raises(RuntimeError, match="200") as exc:
        await GithubStorage().load_range("blob.bin", 0, 4)

    assert f"raw.githubusercontent.com/acme/content/{HEAD_SHA}/blob.bin" in str(exc.value)
    assert resp.body_read is False


async def test_load_range_encoded_answer_raises_without_reading_the_body(client):
    # A compressed answer ranges over the encoded bytes, not the object's.
    client.get.return_value = _ref_answer()
    resp = make_response(
        status=206,
        content=b"\x1f\x8b\x08\x00\x00",
        headers={"Content-Range": "bytes 0-4/47", "Content-Encoding": "gzip"},
    )
    set_stream(client, resp)

    with pytest.raises(RuntimeError, match="gzip") as exc:
        await GithubStorage().load_range("blob.bin", 0, 5)

    assert f"raw.githubusercontent.com/acme/content/{HEAD_SHA}/blob.bin" in str(exc.value)
    assert resp.body_read is False


async def test_load_range_longer_answer_than_asked_raises(client):
    client.get.return_value = _ref_answer()
    set_stream(client, make_response(status=206, chunks=[b"abc", b"def"]))

    with pytest.raises(RuntimeError, match="more than the 4 bytes"):
        await GithubStorage().load_range("blob.bin", 0, 4)


async def test_load_range_server_error_propagates(client):
    client.get.return_value = _ref_answer()
    set_stream(client, make_response(status=500, text="boom"))

    with pytest.raises(HTTPStatusError):
        await GithubStorage().load_range("blob.bin", 0, 4)


async def test_load_range_unexpected_success_status_raises(client):
    client.get.return_value = _ref_answer()
    set_stream(client, make_response(status=204))

    with pytest.raises(RuntimeError, match="204"):
        await GithubStorage().load_range("blob.bin", 0, 4)


async def test_load_range_failed_head_lookup_raises_before_the_read(client):
    client.get.return_value = make_response(status=404)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().load_range("blob.bin", 0, 4)

    client.stream.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"ref": "refs/heads/dev"},
        {"object": {"type": "commit"}},
        {"object": {"sha": ""}},
        {"object": {"sha": 7}},
        {"object": "abc"},
        [{"ref": "refs/heads/dev-a"}],
    ],
    ids=["no-object", "no-sha", "empty-sha", "int-sha", "object-not-a-dict", "list"],
)
async def test_load_range_head_answer_without_a_commit_sha_raises(client, body):
    client.get.return_value = make_response(status=200, json_body=body)

    with pytest.raises(RuntimeError, match="git/ref/heads/dev"):
        await GithubStorage().load_range("blob.bin", 0, 4)

    client.stream.assert_not_called()


async def test_load_range_zero_length_sends_no_request(client):
    assert await GithubStorage().load_range("blob.bin", 0, 0) == b""

    client.get.assert_not_called()
    client.stream.assert_not_called()


async def test_load_range_resolves_the_head_on_every_call(client):
    shas = ["a" * 40, "b" * 40]
    client.get.side_effect = [_ref_answer(shas[0]), _ref_answer(shas[1])]
    set_stream(client, make_response(status=206, content=b"x"))

    await GithubStorage().load_range("p", 0, 1)
    await GithubStorage().load_range("p", 0, 1)

    urls = [c.args[1] for c in client.stream.call_args_list]
    assert urls == [f"https://raw.githubusercontent.com/acme/content/{sha}/p" for sha in shas]
