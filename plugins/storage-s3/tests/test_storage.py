"""Unit tests for the full ``S3Storage`` surface.

The S3 client is mocked (no live bucket): each test wires the mock's return values
or side-effects, then asserts the backend's call shape and its error contract —
notably that a missing object maps to ``FileNotFoundError`` (never a raw
``ClientError``) and that a delete-root escape is refused.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from tai42_contract.storage import ObjectStat, Storage

from tai42_storage_s3 import S3Storage
from tests.conftest import FakeBody, FakePaginator, forbidden_error, not_found_error


def test_s3storage_is_registered_storage_subclass() -> None:
    # Importing the package fired @register_storage; the class is a Storage.
    assert issubclass(S3Storage, Storage)


def test_import_registers_provider_as_side_effect() -> None:
    # Importing the package must register S3Storage via the decorator; the stub
    # facet records the class it was handed.
    from tests.conftest import _stub_app

    assert _stub_app.storage.registered is S3Storage


def test_settings_env_prefix_is_storage_s3() -> None:
    from tai42_storage_s3.settings import S3Settings

    assert S3Settings.model_config.get("env_prefix") == "STORAGE_S3_"


def test_settings_accessor_is_cached() -> None:
    from tai42_storage_s3.settings import S3Settings, s3_settings

    first = s3_settings()
    assert isinstance(first, S3Settings)
    # ``settings_cache`` memoizes the zero-arg accessor.
    assert s3_settings() is first


# --- bucket config guard -----------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        lambda s: s.load("x.j2"),
        lambda s: s.load_bytes("x.bin"),
        lambda s: s.list(),
        lambda s: s.upload("x.j2", "content"),
        lambda s: s.upload_bytes("x.bin", b"data"),
        lambda s: s.delete("x.j2"),
        lambda s: s.delete_dir("d"),
        lambda s: s.stat("x.j2"),
    ],
    ids=["load", "load_bytes", "list", "upload", "upload_bytes", "delete", "delete_dir", "stat"],
)
async def test_unset_bucket_raises_config_error(
    s3_client: Any, monkeypatch: pytest.MonkeyPatch, operation: Any
) -> None:
    # An unset bucket must surface as a config error naming the env var — before
    # any client call — not as a deep botocore validation error.
    from types import SimpleNamespace

    from tai42_storage_s3 import storage as storage_module

    monkeypatch.setattr(storage_module, "s3_settings", lambda: SimpleNamespace(bucket=None))

    with pytest.raises(RuntimeError, match="STORAGE_S3_BUCKET"):
        await operation(S3Storage())

    assert not s3_client.method_calls


# --- load / load_bytes -------------------------------------------------------


async def test_load_decodes_bytes(s3_client: Any) -> None:
    s3_client.get_object.return_value = {"Body": FakeBody("héllo".encode())}

    result = await S3Storage().load("greeting.j2")

    assert result == "héllo"
    s3_client.get_object.assert_awaited_once_with(Bucket="b", Key="greeting.j2")


async def test_load_bytes_returns_raw_bytes(s3_client: Any) -> None:
    payload = bytes(range(256))
    s3_client.get_object.return_value = {"Body": FakeBody(payload)}

    result = await S3Storage().load_bytes("blob.bin")

    assert result == payload


async def test_load_missing_raises_filenotfound(s3_client: Any) -> None:
    s3_client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")

    with pytest.raises(FileNotFoundError):
        await S3Storage().load("missing.j2")


async def test_load_bytes_missing_raises_filenotfound(s3_client: Any) -> None:
    s3_client.get_object.side_effect = not_found_error("GetObject")

    with pytest.raises(FileNotFoundError):
        await S3Storage().load_bytes("missing.bin")


async def test_load_other_clienterror_propagates(s3_client: Any) -> None:
    s3_client.get_object.side_effect = forbidden_error("GetObject")

    with pytest.raises(ClientError):
        await S3Storage().load("x.j2")


# --- list --------------------------------------------------------------------


async def test_list_collects_keys_across_pages(s3_client: Any) -> None:
    pages = [
        {"Contents": [{"Key": "a.j2"}, {"Key": "b.j2"}]},
        {"Contents": [{"Key": "c.j2"}]},
    ]
    s3_client.get_paginator.return_value = FakePaginator(pages)

    keys = await S3Storage().list()

    assert keys == ["a.j2", "b.j2", "c.j2"]


async def test_list_empty_bucket_returns_empty(s3_client: Any) -> None:
    # A page with no "Contents" key must not raise.
    s3_client.get_paginator.return_value = FakePaginator([{}])

    assert await S3Storage().list() == []


# --- upload / upload_bytes ---------------------------------------------------


async def test_upload_stores_jinja2_content_type(s3_client: Any) -> None:
    await S3Storage().upload("t.j2", "{{ name }}")

    s3_client.put_object.assert_awaited_once_with(
        Bucket="b", Key="t.j2", Body=b"{{ name }}", ContentType="application/jinja2"
    )


async def test_upload_bytes_passes_parametrized_content_type(s3_client: Any) -> None:
    await S3Storage().upload_bytes("pic.png", b"\x89PNG", content_type="image/png")

    s3_client.put_object.assert_awaited_once_with(Bucket="b", Key="pic.png", Body=b"\x89PNG", ContentType="image/png")


async def test_upload_bytes_omits_content_type_when_none(s3_client: Any) -> None:
    await S3Storage().upload_bytes("blob.bin", b"\x00\x01")

    s3_client.put_object.assert_awaited_once_with(Bucket="b", Key="blob.bin", Body=b"\x00\x01")


async def test_upload_bytes_load_bytes_roundtrip_is_identical(s3_client: Any) -> None:
    store: dict[str, bytes] = {}

    async def _put(**kwargs: Any) -> None:
        store[kwargs["Key"]] = kwargs["Body"]

    async def _get(**kwargs: Any) -> dict[str, Any]:
        return {"Body": FakeBody(store[kwargs["Key"]])}

    s3_client.put_object.side_effect = _put
    s3_client.get_object.side_effect = _get

    payload = bytes(range(256))
    storage = S3Storage()
    await storage.upload_bytes("full.bin", payload, content_type="application/octet-stream")

    assert await storage.load_bytes("full.bin") == payload


# --- delete ------------------------------------------------------------------


async def test_delete_existing_key(s3_client: Any) -> None:
    s3_client.head_object.return_value = {}

    await S3Storage().delete("a/b.j2")

    s3_client.head_object.assert_awaited_once_with(Bucket="b", Key="a/b.j2")
    s3_client.delete_object.assert_awaited_once_with(Bucket="b", Key="a/b.j2")


async def test_delete_missing_raises_filenotfound(s3_client: Any) -> None:
    s3_client.head_object.side_effect = not_found_error("HeadObject")

    with pytest.raises(FileNotFoundError):
        await S3Storage().delete("gone.j2")

    s3_client.delete_object.assert_not_called()


async def test_delete_non_404_clienterror_propagates(s3_client: Any) -> None:
    s3_client.head_object.side_effect = forbidden_error("HeadObject")

    with pytest.raises(ClientError):
        await S3Storage().delete("a/b.j2")

    s3_client.delete_object.assert_not_called()


# --- delete_dir --------------------------------------------------------------


@pytest.mark.parametrize("root", ["", "/", ".", "  ", "//", "a/..", "../x", "/.."])
async def test_delete_dir_root_escape_refused(s3_client: Any, root: str) -> None:
    with pytest.raises(ValueError, match="Refusing to delete the storage root"):
        await S3Storage().delete_dir(root)


async def test_delete_dir_deletes_under_prefix(s3_client: Any) -> None:
    paginator = FakePaginator([{"Contents": [{"Key": "d/a.j2"}, {"Key": "d/b.j2"}]}])
    s3_client.get_paginator.return_value = paginator
    s3_client.delete_objects.return_value = {}

    await S3Storage().delete_dir("d")

    # The trailing-slash prefix stops "d" also matching a sibling like "d2/x".
    assert paginator.paginate_kwargs == {"Bucket": "b", "Prefix": "d/"}
    _, kwargs = s3_client.delete_objects.call_args
    assert kwargs["Bucket"] == "b"
    assert kwargs["Delete"]["Objects"] == [{"Key": "d/a.j2"}, {"Key": "d/b.j2"}]


async def test_delete_dir_keeps_existing_trailing_slash(s3_client: Any) -> None:
    paginator = FakePaginator([{"Contents": [{"Key": "d/a.j2"}]}])
    s3_client.get_paginator.return_value = paginator
    s3_client.delete_objects.return_value = {}

    await S3Storage().delete_dir("d/")

    # An already-slashed path must not become "d//".
    assert paginator.paginate_kwargs is not None
    assert paginator.paginate_kwargs["Prefix"] == "d/"


async def test_delete_dir_chunks_over_1000_keys(s3_client: Any) -> None:
    page1 = {"Contents": [{"Key": f"d/{i}.j2"} for i in range(1000)]}
    page2 = {"Contents": [{"Key": f"d/{i}.j2"} for i in range(1000, 1500)]}
    s3_client.get_paginator.return_value = FakePaginator([page1, page2])
    s3_client.delete_objects.return_value = {}

    await S3Storage().delete_dir("d")

    assert s3_client.delete_objects.await_count == 2
    sizes = [len(c.kwargs["Delete"]["Objects"]) for c in s3_client.delete_objects.await_args_list]
    assert sizes == [1000, 500]


async def test_delete_dir_empty_raises_filenotfound(s3_client: Any) -> None:
    s3_client.get_paginator.return_value = FakePaginator([{}])

    with pytest.raises(FileNotFoundError):
        await S3Storage().delete_dir("d")


async def test_delete_dir_surfaces_partial_errors(s3_client: Any) -> None:
    s3_client.get_paginator.return_value = FakePaginator([{"Contents": [{"Key": "d/a.j2"}]}])
    s3_client.delete_objects.return_value = {"Errors": [{"Key": "d/a.j2", "Message": "denied"}]}

    with pytest.raises(RuntimeError):
        await S3Storage().delete_dir("d")


# --- stat --------------------------------------------------------------------


async def test_stat_returns_stored_content_type(s3_client: Any) -> None:
    # The stored ContentType differs from what the ".png" suffix would infer,
    # proving stat reports the stored metadata, not path inference.
    s3_client.head_object.return_value = {"ContentType": "application/octet-stream"}

    result = await S3Storage().stat("pic.png")

    assert result == ObjectStat(content_type="application/octet-stream")
    s3_client.head_object.assert_awaited_once_with(Bucket="b", Key="pic.png")


async def test_stat_missing_content_type_is_none(s3_client: Any) -> None:
    s3_client.head_object.return_value = {}

    assert await S3Storage().stat("x") == ObjectStat(content_type=None)


async def test_stat_missing_object_raises_filenotfound(s3_client: Any) -> None:
    s3_client.head_object.side_effect = not_found_error("HeadObject")

    with pytest.raises(FileNotFoundError):
        await S3Storage().stat("gone.png")


async def test_stat_non_404_clienterror_propagates(s3_client: Any) -> None:
    s3_client.head_object.side_effect = forbidden_error("HeadObject")

    with pytest.raises(ClientError):
        await S3Storage().stat("x.png")
