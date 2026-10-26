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
from tai42_contract.storage import ObjectStat, Storage, StoredObject

from tai42_storage_s3 import S3Storage

from .conftest import FakeBody, FakePaginator, forbidden_error, not_found_error


def test_s3storage_is_registered_storage_subclass() -> None:
    # Importing the package fired @register_storage; the class is a Storage.
    assert issubclass(S3Storage, Storage)


def test_import_registers_provider_as_side_effect() -> None:
    # Importing the package must register S3Storage via the decorator; the stub
    # facet records the class it was handed.
    from .conftest import _stub_app

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
        lambda s: s.load_range("x.bin", 0, 4),
    ],
    ids=["load", "load_bytes", "list", "upload", "upload_bytes", "delete", "delete_dir", "stat", "load_range"],
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


async def test_load_object_answers_the_bytes_and_stored_type_from_one_get_object(s3_client: Any) -> None:
    s3_client.get_object.return_value = {"Body": FakeBody(b"\x89PNG"), "ContentType": "image/png"}

    stored = await S3Storage().load_object("img/a.png")

    assert stored == StoredObject(data=b"\x89PNG", content_type="image/png")
    s3_client.get_object.assert_awaited_once_with(Bucket="b", Key="img/a.png")
    s3_client.head_object.assert_not_awaited()


async def test_load_object_missing_raises_filenotfound(s3_client: Any) -> None:
    s3_client.get_object.side_effect = not_found_error("GetObject")

    with pytest.raises(FileNotFoundError):
        await S3Storage().load_object("missing.bin")


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


async def test_list_unscoped_pages_the_whole_bucket(s3_client: Any) -> None:
    paginator = FakePaginator([{}])
    s3_client.get_paginator.return_value = paginator

    await S3Storage().list()

    assert paginator.paginate_kwargs == {"Bucket": "b"}


@pytest.mark.parametrize("directory", ["d/e", "d/e/"])
async def test_list_of_a_directory_pages_only_its_prefix(s3_client: Any, directory: str) -> None:
    paginator = FakePaginator([{"Contents": [{"Key": "d/e/a.j2"}, {"Key": "d/e/f/b.j2"}]}])
    s3_client.get_paginator.return_value = paginator

    assert await S3Storage().list(directory) == ["d/e/a.j2", "d/e/f/b.j2"]
    assert paginator.paginate_kwargs == {"Bucket": "b", "Prefix": "d/e/"}


async def test_list_of_a_blank_directory_is_refused_before_any_call(s3_client: Any) -> None:
    with pytest.raises(ValueError, match="A directory path is required"):
        await S3Storage().list("/")
    s3_client.get_paginator.assert_not_called()


async def test_list_empty_bucket_returns_empty(s3_client: Any) -> None:
    # A page with no "Contents" key must not raise.
    s3_client.get_paginator.return_value = FakePaginator([{}])

    assert await S3Storage().list() == []


# --- upload / upload_bytes ---------------------------------------------------


async def test_upload_stores_utf8_text_content_type(s3_client: Any) -> None:
    # Every upload lists its ``id + "/"`` prefix first to refuse a file/dir collision.
    s3_client.get_paginator.return_value = FakePaginator([{}])

    await S3Storage().upload("t.j2", "{{ name }}")

    s3_client.put_object.assert_awaited_once_with(
        Bucket="b", Key="t.j2", Body=b"{{ name }}", ContentType="text/plain; charset=utf-8"
    )


async def test_upload_bytes_passes_parametrized_content_type(s3_client: Any) -> None:
    s3_client.get_paginator.return_value = FakePaginator([{}])

    await S3Storage().upload_bytes("pic.png", b"\x89PNG", content_type="image/png")

    s3_client.put_object.assert_awaited_once_with(Bucket="b", Key="pic.png", Body=b"\x89PNG", ContentType="image/png")


async def test_upload_bytes_omits_content_type_when_none(s3_client: Any) -> None:
    s3_client.get_paginator.return_value = FakePaginator([{}])

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
    s3_client.get_paginator.return_value = FakePaginator([{}])

    payload = bytes(range(256))
    storage = S3Storage()
    await storage.upload_bytes("full.bin", payload, content_type="application/octet-stream")

    assert await storage.load_bytes("full.bin") == payload


# --- upload path-conflict guard ----------------------------------------------


async def test_upload_prefix_collision_refused(s3_client: Any) -> None:
    from tai42_contract.storage import StoragePathConflictError

    # Keys already live under "a/b/", so "a/b" can't also become a file.
    paginator = FakePaginator([{"Contents": [{"Key": "a/b/c.j2"}, {"Key": "a/b/d.j2"}]}])
    s3_client.get_paginator.return_value = paginator

    with pytest.raises(StoragePathConflictError) as exc:
        await S3Storage().upload("a/b", "cannot")

    assert paginator.paginate_kwargs == {"Bucket": "b", "Prefix": "a/b/"}
    assert exc.value.conflicts == ["a/b/c.j2", "a/b/d.j2"]
    s3_client.put_object.assert_not_called()


async def test_upload_under_existing_file_key_refused(s3_client: Any) -> None:
    from tai42_contract.storage import StoragePathConflictError

    # No keys under "a/b/c/", but the ancestor "a/b" is itself a stored file key.
    s3_client.get_paginator.return_value = FakePaginator([{}])

    async def _head(**kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"] == "a/b":
            return {}
        raise not_found_error("HeadObject")

    s3_client.head_object.side_effect = _head

    with pytest.raises(StoragePathConflictError) as exc:
        await S3Storage().upload("a/b/c.j2", "nested")

    assert exc.value.conflicts == ["a/b"]
    s3_client.put_object.assert_not_called()


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


async def test_stat_returns_stored_content_type_and_size(s3_client: Any) -> None:
    # The stored ContentType differs from what the ".png" suffix would infer,
    # proving stat reports the stored metadata, not path inference.
    s3_client.head_object.return_value = {"ContentType": "application/octet-stream", "ContentLength": 26214400}

    result = await S3Storage().stat("pic.png")

    assert result == ObjectStat(content_type="application/octet-stream", size=26214400)
    s3_client.head_object.assert_awaited_once_with(Bucket="b", Key="pic.png")


async def test_stat_missing_content_type_is_none(s3_client: Any) -> None:
    s3_client.head_object.return_value = {"ContentLength": 3}

    assert await S3Storage().stat("x") == ObjectStat(content_type=None, size=3)


async def test_stat_missing_object_raises_filenotfound(s3_client: Any) -> None:
    s3_client.head_object.side_effect = not_found_error("HeadObject")

    with pytest.raises(FileNotFoundError):
        await S3Storage().stat("gone.png")


async def test_stat_non_404_clienterror_propagates(s3_client: Any) -> None:
    s3_client.head_object.side_effect = forbidden_error("HeadObject")

    with pytest.raises(ClientError):
        await S3Storage().stat("x.png")


# --- load_range --------------------------------------------------------------


def invalid_range_error() -> Any:
    return ClientError({"Error": {"Code": "InvalidRange", "Message": "Requested Range Not Satisfiable"}}, "GetObject")


async def test_load_range_sends_the_byte_range_and_answers_the_body(s3_client: Any) -> None:
    s3_client.get_object.return_value = {"Body": FakeBody(b"\x89PNG")}

    assert await S3Storage().load_range("pic.png", 0, 4) == b"\x89PNG"
    s3_client.get_object.assert_awaited_once_with(Bucket="b", Key="pic.png", Range="bytes=0-3")


async def test_load_range_of_the_tail_sends_the_tail_range(s3_client: Any) -> None:
    s3_client.get_object.return_value = {"Body": FakeBody(b"tail")}

    assert await S3Storage().load_range("blob.bin", 96, 4) == b"tail"
    s3_client.get_object.assert_awaited_once_with(Bucket="b", Key="blob.bin", Range="bytes=96-99")


async def test_load_range_running_past_the_end_answers_the_tail_only(s3_client: Any) -> None:
    # S3 clamps a range whose end is past the object to the bytes it has.
    s3_client.get_object.return_value = {"Body": FakeBody(b"ab")}

    assert await S3Storage().load_range("blob.bin", 98, 10) == b"ab"
    s3_client.get_object.assert_awaited_once_with(Bucket="b", Key="blob.bin", Range="bytes=98-107")


async def test_load_range_past_the_end_answers_empty(s3_client: Any) -> None:
    s3_client.get_object.side_effect = invalid_range_error()

    assert await S3Storage().load_range("blob.bin", 100, 4) == b""


async def test_load_range_zero_length_answers_empty_without_a_call(s3_client: Any) -> None:
    assert await S3Storage().load_range("blob.bin", 0, 0) == b""
    s3_client.get_object.assert_not_called()


async def test_load_range_missing_object_raises_filenotfound(s3_client: Any) -> None:
    s3_client.get_object.side_effect = not_found_error("GetObject")

    with pytest.raises(FileNotFoundError, match=r"gone\.bin"):
        await S3Storage().load_range("gone.bin", 0, 4)


async def test_load_range_other_clienterror_propagates(s3_client: Any) -> None:
    s3_client.get_object.side_effect = forbidden_error("GetObject")

    with pytest.raises(ClientError):
        await S3Storage().load_range("x.bin", 0, 4)


# --- checksum algorithm for S3-compatible stores -----------------------------
# botocore signs an ``x-amz-checksum-crc32`` header on the request body of
# ``delete_objects`` (always, the operation requires a checksum) and ``put_object``
# (the ``aws-chunked`` trailer). An S3-compatible store that refuses CRC32 500s
# such a request; ``STORAGE_S3_CHECKSUM_ALGORITHM`` lets the deployment pick an
# algorithm the store accepts, sent as the operation's ``ChecksumAlgorithm``.


def _settings_with_checksum(algorithm: Any) -> Any:
    from types import SimpleNamespace

    return lambda: SimpleNamespace(bucket="b", checksum_algorithm=algorithm)


async def test_delete_dir_sends_configured_checksum_algorithm(s3_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_storage_s3 import storage as storage_module

    monkeypatch.setattr(storage_module, "s3_settings", _settings_with_checksum("SHA256"))
    s3_client.get_paginator.return_value = FakePaginator([{"Contents": [{"Key": "d/a.j2"}]}])
    s3_client.delete_objects.return_value = {}

    await S3Storage().delete_dir("d")

    _, kwargs = s3_client.delete_objects.call_args
    assert kwargs["ChecksumAlgorithm"] == "SHA256"


async def test_delete_dir_omits_checksum_algorithm_by_default(s3_client: Any) -> None:
    # Unset default keeps botocore's own behaviour so real AWS S3 is unchanged.
    s3_client.get_paginator.return_value = FakePaginator([{"Contents": [{"Key": "d/a.j2"}]}])
    s3_client.delete_objects.return_value = {}

    await S3Storage().delete_dir("d")

    _, kwargs = s3_client.delete_objects.call_args
    assert "ChecksumAlgorithm" not in kwargs


async def test_upload_bytes_omits_checksum_algorithm_even_when_configured(
    s3_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # put_object does not require a request-body checksum; an explicit ChecksumAlgorithm would make
    # botocore switch to aws-chunked streaming, which not every S3-compatible store implements. The
    # configured algorithm rides delete_objects (which the API requires one on), never an upload.
    from tai42_storage_s3 import storage as storage_module

    monkeypatch.setattr(storage_module, "s3_settings", _settings_with_checksum("SHA256"))
    s3_client.get_paginator.return_value = FakePaginator([{}])

    await S3Storage().upload_bytes("pic.png", b"\x89PNG", content_type="image/png")

    _, kwargs = s3_client.put_object.call_args
    assert "ChecksumAlgorithm" not in kwargs


async def test_upload_bytes_omits_checksum_algorithm_by_default(s3_client: Any) -> None:
    s3_client.get_paginator.return_value = FakePaginator([{}])

    await S3Storage().upload_bytes("blob.bin", b"\x00\x01")

    _, kwargs = s3_client.put_object.call_args
    assert "ChecksumAlgorithm" not in kwargs
