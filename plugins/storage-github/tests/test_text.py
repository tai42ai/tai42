"""The five text methods: load / list / upload / delete / delete_dir."""

from __future__ import annotations

import base64

import pytest
from httpx import HTTPStatusError

from tai42_storage_github import GithubStorage

from .conftest import make_response

pytestmark = pytest.mark.usefixtures("client")


RAW_FORM = "application/vnd.github.raw+json"
RAW_FORM_HEADERS = {"Content-Type": f"{RAW_FORM}; charset=utf-8"}


async def test_load_reads_the_raw_form_from_the_contents_api_at_the_branch(client):
    client.get.return_value = make_response(status=200, text="hello world", headers=RAW_FORM_HEADERS)

    result = await GithubStorage().load("greetings/hi.j2")

    assert result == "hello world"
    client.get.assert_awaited_once()
    args, kwargs = client.get.call_args
    assert args[0] == "https://api.github.com/repos/acme/content/contents/greetings/hi.j2"
    assert kwargs["params"] == {"ref": "dev"}
    assert kwargs["headers"]["Accept"] == RAW_FORM
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert "Range" not in kwargs["headers"]
    client.stream.assert_not_called()


async def test_load_non_utf8_object_raises_unicode_decode_error(client):
    # httpx's ``Response.text`` would substitute U+FFFD for the invalid bytes; ``load``
    # strictly decodes the raw bytes so a binary object is refused, never garbled.
    png = b"\x89PNG\r\n\x1a\n"
    client.get.return_value = make_response(
        status=200, text=png.decode("utf-8", errors="replace"), content=png, headers=RAW_FORM_HEADERS
    )

    with pytest.raises(UnicodeDecodeError):
        await GithubStorage().load("images/logo.png")


async def test_load_missing_raises_filenotfound(client):
    client.get.return_value = make_response(status=404)

    with pytest.raises(FileNotFoundError):
        await GithubStorage().load("missing.j2")


async def test_load_of_a_directory_listing_raises_filenotfound(client):
    client.get.return_value = make_response(
        status=200, text="[]", headers={"Content-Type": "application/json; charset=utf-8"}
    )

    with pytest.raises(FileNotFoundError, match="Object not found: d"):
        await GithubStorage().load("d")


async def test_load_of_another_content_type_raises_naming_it(client):
    client.get.return_value = make_response(status=200, text="<html>", headers={"Content-Type": "text/html"})

    with pytest.raises(RuntimeError, match="text/html"):
        await GithubStorage().load("x.j2")


async def test_load_server_error_propagates(client):
    client.get.return_value = make_response(status=500)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().load("x.j2")


async def test_list_returns_blob_paths_via_trees(client):
    client.get.return_value = make_response(
        status=200,
        json_body={
            "truncated": False,
            "tree": [
                {"path": "a.j2", "type": "blob", "sha": "s1"},
                {"path": "d", "type": "tree", "sha": "t1"},
                {"path": "d/b.j2", "type": "blob", "sha": "s2"},
            ],
        },
    )

    files = await GithubStorage().list()

    assert files == ["a.j2", "d/b.j2"]
    assert client.get.call_args.kwargs["params"] == {"recursive": "1"}
    assert client.get.call_args.args[0].endswith("/git/trees/dev")


async def test_list_empty_tree_returns_empty(client):
    client.get.return_value = make_response(status=200, json_body={"truncated": False})

    assert await GithubStorage().list() == []


async def test_list_truncated_raises(client):
    client.get.return_value = make_response(status=200, json_body={"truncated": True, "tree": []})

    with pytest.raises(RuntimeError, match="truncated"):
        await GithubStorage().list()


async def test_list_non_dict_response_raises(client):
    client.get.return_value = make_response(status=200, json_body=[1, 2, 3])

    with pytest.raises(RuntimeError):
        await GithubStorage().list()


async def test_list_server_error_propagates(client):
    client.get.return_value = make_response(status=500)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().list()


TREES = "https://api.github.com/repos/acme/content/git/trees"
REF_URL = "https://api.github.com/repos/acme/content/git/ref/heads/dev"


@pytest.mark.parametrize("directory", ["d/sub", "d/sub/", "/d/sub"])
async def test_list_of_a_directory_reads_only_its_tree_and_answers_full_paths(client, directory):
    client.get.return_value = make_response(
        status=200,
        json_body={
            "truncated": False,
            "tree": [
                {"path": "a.j2", "type": "blob", "sha": "s1"},
                {"path": "e", "type": "tree", "sha": "t1"},
                {"path": "e/b.j2", "type": "blob", "sha": "s2"},
            ],
        },
    )

    files = await GithubStorage().list(directory)

    assert files == ["d/sub/a.j2", "d/sub/e/b.j2"]
    assert client.get.await_count == 1
    assert client.get.call_args.args[0] == f"{TREES}/dev:d/sub"
    assert client.get.call_args.kwargs["params"] == {"recursive": "1"}


async def test_list_of_a_directory_quotes_its_path_into_the_tree(client):
    client.get.return_value = make_response(status=200, json_body={"truncated": False, "tree": []})

    await GithubStorage().list("my dir/#1")

    assert client.get.call_args.args[0] == f"{TREES}/dev:my%20dir/%231"


async def test_list_of_a_missing_directory_confirms_the_branch_and_is_empty(client):
    client.get.side_effect = [
        make_response(status=404),
        make_response(status=200, json_body={"object": {"sha": "abc123"}}),
    ]

    assert await GithubStorage().list("absent") == []
    assert [call.args[0] for call in client.get.call_args_list] == [f"{TREES}/dev:absent", REF_URL]


async def test_list_of_a_directory_on_a_missing_branch_raises(client):
    client.get.side_effect = [make_response(status=404), make_response(status=404)]

    with pytest.raises(HTTPStatusError):
        await GithubStorage().list("d")


async def test_list_of_a_path_naming_a_file_is_empty(client):
    client.get.return_value = make_response(status=422)

    assert await GithubStorage().list("a.j2") == []


async def test_list_of_a_directory_server_error_propagates(client):
    client.get.return_value = make_response(status=500)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().list("d")


async def test_list_of_a_directory_with_a_parent_segment_is_refused_before_any_call(client):
    with pytest.raises(ValueError, match="path traversal"):
        await GithubStorage().list("d/../e")
    client.get.assert_not_called()


async def test_upload_create_puts_without_sha(client):
    client.get.return_value = make_response(status=404)
    client.put.return_value = make_response(status=200)

    await GithubStorage().upload("a/b.j2", "hello")

    payload = client.put.call_args.kwargs["json"]
    assert "sha" not in payload
    assert base64.b64decode(payload["content"]) == b"hello"
    assert payload["branch"] == "dev"


async def test_upload_update_reuses_existing_sha(client):
    client.get.return_value = make_response(status=200, json_body={"sha": "deadbeef"})
    client.put.return_value = make_response(status=200)

    await GithubStorage().upload("a/b.j2", "hello")

    assert client.put.call_args.kwargs["json"]["sha"] == "deadbeef"


async def test_upload_onto_directory_path_refused(client):
    from tai42_contract.storage import StoragePathConflictError

    # The Contents API returns an ARRAY for a directory path: the id names a
    # directory that holds objects, so it can't also become a file.
    client.get.return_value = make_response(
        status=200,
        json_body=[
            {"path": "a/b/c.j2", "type": "file"},
            {"path": "a/b/d.j2", "type": "file"},
        ],
    )

    with pytest.raises(StoragePathConflictError) as exc:
        await GithubStorage().upload("a/b", "hello")

    assert exc.value.conflicts == ["a/b/c.j2", "a/b/d.j2"]
    client.put.assert_not_called()


async def test_upload_lookup_error_propagates(client):
    client.get.return_value = make_response(status=500)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().upload("a/b.j2", "hello")

    client.put.assert_not_called()


async def test_upload_put_error_propagates(client):
    client.get.return_value = make_response(status=404)
    client.put.return_value = make_response(status=422)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().upload("a/b.j2", "hello")


async def test_delete_resolves_sha_then_deletes(client):
    client.get.return_value = make_response(status=200, json_body={"sha": "abc123"})
    client.request.return_value = make_response(status=200)

    await GithubStorage().delete("a/b.j2")

    args, kwargs = client.request.call_args
    assert args[0] == "DELETE"
    assert args[1].endswith("a/b.j2")
    assert kwargs["json"]["sha"] == "abc123"
    assert kwargs["json"]["branch"] == "dev"


async def test_delete_missing_raises_filenotfound(client):
    client.get.return_value = make_response(status=404)

    with pytest.raises(FileNotFoundError):
        await GithubStorage().delete("missing.j2")

    client.request.assert_not_called()


async def test_delete_lookup_error_propagates(client):
    client.get.return_value = make_response(status=500)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().delete("a/b.j2")

    client.request.assert_not_called()


async def test_delete_file_without_sha_raises_before_request(client):
    # A file object carrying no sha cannot be deleted — raise locally with a
    # clear message instead of deferring to the API's opaque 422.
    client.get.return_value = make_response(status=200, json_body={"name": "b.j2"})

    with pytest.raises(RuntimeError, match="no blob sha"):
        await GithubStorage().delete("a/b.j2")

    client.request.assert_not_called()


async def test_delete_on_directory_path_raises_filenotfound(client):
    # A directory path returns a list, not a file object — no blob sha to delete.
    client.get.return_value = make_response(status=200, json_body=[{"name": "x.j2"}])

    with pytest.raises(FileNotFoundError):
        await GithubStorage().delete("a/dir")

    client.request.assert_not_called()


async def test_delete_of_a_file_gone_before_its_delete_raises_filenotfound(client):
    client.get.return_value = make_response(status=200, json_body={"sha": "abc"})
    client.request.return_value = make_response(status=404)

    with pytest.raises(FileNotFoundError):
        await GithubStorage().delete("a/b.j2")


async def test_delete_call_error_propagates(client):
    client.get.return_value = make_response(status=200, json_body={"sha": "abc"})
    client.request.return_value = make_response(status=422)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().delete("a/b.j2")
