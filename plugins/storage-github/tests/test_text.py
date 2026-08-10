"""The five text methods: load / list / upload / delete / delete_dir."""

from __future__ import annotations

import base64

import pytest
from httpx import HTTPStatusError

from tai42_storage_github import GithubStorage
from tests.conftest import make_response

pytestmark = pytest.mark.usefixtures("client")


async def test_load_returns_text_from_raw_endpoint(client):
    client.get.return_value = make_response(status=200, text="hello world")

    result = await GithubStorage().load("greetings/hi.j2")

    assert result == "hello world"
    url = client.get.call_args.args[0]
    assert url == "https://raw.githubusercontent.com/acme/content/refs/heads/main/greetings/hi.j2"


async def test_load_missing_raises_filenotfound(client):
    client.get.return_value = make_response(status=404)

    with pytest.raises(FileNotFoundError):
        await GithubStorage().load("missing.j2")


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
                {"path": "a.j2", "type": "blob"},
                {"path": "d", "type": "tree"},
                {"path": "d/b.j2", "type": "blob"},
            ],
        },
    )

    files = await GithubStorage().list()

    assert files == ["a.j2", "d/b.j2"]
    assert client.get.call_args.kwargs["params"] == {"recursive": "1"}
    assert client.get.call_args.args[0].endswith("/git/trees/main")


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


async def test_list_blobs_prefix_filters_and_excludes_trees(client):
    client.get.return_value = make_response(
        status=200,
        json_body={
            "truncated": False,
            "tree": [
                {"path": "d/a.j2", "type": "blob"},
                {"path": "d/sub", "type": "tree"},
                {"path": "d/sub/b.j2", "type": "blob"},
                {"path": "dd/c.j2", "type": "blob"},
            ],
        },
    )

    files = await GithubStorage()._list_blobs("d/")

    assert files == ["d/a.j2", "d/sub/b.j2"]


async def test_upload_create_puts_without_sha(client):
    client.get.return_value = make_response(status=404)
    client.put.return_value = make_response(status=200)

    await GithubStorage().upload("a/b.j2", "hello")

    payload = client.put.call_args.kwargs["json"]
    assert "sha" not in payload
    assert base64.b64decode(payload["content"]) == b"hello"
    assert payload["branch"] == "main"


async def test_upload_update_reuses_existing_sha(client):
    client.get.return_value = make_response(status=200, json_body={"sha": "deadbeef"})
    client.put.return_value = make_response(status=200)

    await GithubStorage().upload("a/b.j2", "hello")

    assert client.put.call_args.kwargs["json"]["sha"] == "deadbeef"


async def test_upload_existing_non_dict_treated_as_create(client):
    client.get.return_value = make_response(status=200, json_body=[{"x": 1}])
    client.put.return_value = make_response(status=200)

    await GithubStorage().upload("a/b.j2", "hello")

    assert "sha" not in client.put.call_args.kwargs["json"]


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
    assert kwargs["json"]["branch"] == "main"


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


async def test_delete_call_error_propagates(client):
    client.get.return_value = make_response(status=200, json_body={"sha": "abc"})
    client.request.return_value = make_response(status=422)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().delete("a/b.j2")
