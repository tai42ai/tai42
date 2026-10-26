"""``delete_dir``: the root guard, one tree listing, a DELETE per blob with its tree sha, and error propagation."""

from __future__ import annotations

import pytest
from httpx import HTTPStatusError

from tai42_storage_github import GithubStorage

from .conftest import make_response

TREE_URL = "https://api.github.com/repos/acme/content/git/trees/dev"
CONTENTS_URL = "https://api.github.com/repos/acme/content/contents"


def _tree(*entries: dict[str, str]):
    return make_response(status=200, json_body={"truncated": False, "tree": list(entries)})


def _deleted(client) -> list[tuple[str, str]]:
    """The ``(url, sha)`` of every DELETE the client was asked to send."""
    deleted = []
    for call in client.request.call_args_list:
        assert call.args[0] == "DELETE"
        deleted.append((call.args[1], call.kwargs["json"]["sha"]))
    return deleted


@pytest.mark.parametrize("root", ["", "/", ".", "  ", "//", "a/..", "./x/../..", "../x", "/.."])
async def test_delete_dir_root_escape_refused(root):
    with pytest.raises(ValueError, match="storage root"):
        await GithubStorage().delete_dir(root)


@pytest.mark.parametrize("path", ["d", "d/"])
async def test_delete_dir_deletes_each_blob_under_it_with_its_tree_sha(client, path):
    client.get.return_value = _tree(
        {"path": "d/a.j2", "type": "blob", "sha": "s1"},
        {"path": "d/sub", "type": "tree", "sha": "t1"},
        {"path": "d/sub/b.j2", "type": "blob", "sha": "s2"},
        {"path": "dd/c.j2", "type": "blob", "sha": "s3"},
        {"path": "e/d.j2", "type": "blob", "sha": "s4"},
    )
    client.request.return_value = make_response(status=200)

    await GithubStorage().delete_dir(path)

    client.get.assert_awaited_once()
    assert client.get.call_args.args[0] == TREE_URL
    assert client.get.call_args.kwargs["params"] == {"recursive": "1"}
    assert _deleted(client) == [(f"{CONTENTS_URL}/d/a.j2", "s1"), (f"{CONTENTS_URL}/d/sub/b.j2", "s2")]
    payload = client.request.call_args.kwargs["json"]
    assert payload == {"message": "Delete d/sub/b.j2", "sha": "s2", "branch": "dev"}


async def test_delete_dir_of_an_empty_listing_raises_filenotfound_and_deletes_nothing(client):
    client.get.return_value = _tree({"path": "e/a.j2", "type": "blob", "sha": "s1"})

    with pytest.raises(FileNotFoundError):
        await GithubStorage().delete_dir("d")

    client.get.assert_awaited_once()
    client.request.assert_not_called()


async def test_delete_dir_skips_a_file_already_gone_and_deletes_the_rest(client):
    client.get.return_value = _tree(
        {"path": "d/a.j2", "type": "blob", "sha": "s1"},
        {"path": "d/b.j2", "type": "blob", "sha": "s2"},
    )
    client.request.side_effect = [make_response(status=404), make_response(status=200)]

    await GithubStorage().delete_dir("d")

    assert _deleted(client) == [(f"{CONTENTS_URL}/d/a.j2", "s1"), (f"{CONTENTS_URL}/d/b.j2", "s2")]


async def test_delete_dir_raises_on_a_file_changed_since_the_listing(client):
    client.get.return_value = _tree(
        {"path": "d/a.j2", "type": "blob", "sha": "s1"},
        {"path": "d/b.j2", "type": "blob", "sha": "s2"},
    )
    client.request.return_value = make_response(status=409)

    with pytest.raises(HTTPStatusError):
        await GithubStorage().delete_dir("d")

    assert client.request.await_count == 1


async def test_delete_dir_raises_on_a_blob_without_a_sha_before_deleting_anything(client):
    client.get.return_value = _tree(
        {"path": "d/a.j2", "type": "blob", "sha": "s1"},
        {"path": "d/b.j2", "type": "blob"},
    )

    with pytest.raises(RuntimeError, match="no blob sha"):
        await GithubStorage().delete_dir("d")

    client.get.assert_awaited_once()
    client.request.assert_not_called()
