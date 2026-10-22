"""Unit tests for the full ``OciStorage`` surface.

The client facade is mocked (no live bucket): each test wires the mock's return
values or side-effects, then asserts the backend's call shape and its error
contract — notably that a missing object maps to ``FileNotFoundError`` (never a
raw ``ServiceError``) and that a delete-root escape is refused.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.storage import ObjectStat, Storage, StoragePathConflictError

from tai42_storage_oci import OciStorage

from .conftest import forbidden_error, not_found_error

BUCKET = "tenant-bucket"


def test_ocistorage_is_registered_storage_subclass() -> None:
    assert issubclass(OciStorage, Storage)


def test_import_registers_provider_as_side_effect() -> None:
    from .conftest import _stub_app

    assert _stub_app.storage.registered is OciStorage


def test_settings_env_prefix_is_storage_oci() -> None:
    from tai42_storage_oci.settings import OciSettings

    assert OciSettings.model_config.get("env_prefix") == "STORAGE_OCI_"


def test_settings_accessor_is_cached() -> None:
    from tai42_storage_oci.settings import OciSettings, oci_settings

    first = oci_settings()
    assert isinstance(first, OciSettings)
    assert oci_settings() is first


def test_settings_default_auth_is_workload_identity() -> None:
    from tai42_storage_oci.settings import oci_settings

    assert oci_settings().auth == "workload_identity"


def test_settings_hold_no_credential_field() -> None:
    from tai42_storage_oci.settings import OciSettings

    # A principal needs no key, so no field may invite one.
    assert not {"access_key", "secret_key", "password", "token"} & set(OciSettings.model_fields)


async def test_unconfigured_bucket_is_a_loud_config_error(
    monkeypatch: pytest.MonkeyPatch, oci_client: Any
) -> None:
    from tai42_storage_oci import settings as settings_module

    monkeypatch.delenv("STORAGE_OCI_BUCKET", raising=False)
    settings_module.oci_settings.cache_clear()
    with pytest.raises(RuntimeError, match="STORAGE_OCI_BUCKET"):
        await OciStorage().list()


async def test_load_bytes_returns_stored_bytes(oci_client: Any) -> None:
    oci_client.get_object.return_value = (b"payload", "application/octet-stream")
    assert await OciStorage().load_bytes("a/b.bin") == b"payload"
    oci_client.get_object.assert_awaited_once_with(BUCKET, "a/b.bin")


async def test_load_decodes_utf8(oci_client: Any) -> None:
    oci_client.get_object.return_value = ("héllo".encode(), "text/plain; charset=utf-8")
    assert await OciStorage().load("greet.txt") == "héllo"


async def test_load_maps_missing_object_to_file_not_found(oci_client: Any) -> None:
    oci_client.get_object.side_effect = not_found_error()
    with pytest.raises(FileNotFoundError, match="missing.txt"):
        await OciStorage().load("missing.txt")


async def test_load_propagates_other_service_errors(oci_client: Any) -> None:
    from oci.exceptions import ServiceError

    oci_client.get_object.side_effect = forbidden_error()
    with pytest.raises(ServiceError):
        await OciStorage().load("denied.txt")


async def test_list_returns_every_name(oci_client: Any) -> None:
    oci_client.list_objects.return_value = ["a.txt", "d/b.txt"]
    assert await OciStorage().list() == ["a.txt", "d/b.txt"]
    oci_client.list_objects.assert_awaited_once_with(BUCKET)


async def test_upload_sends_text_content_type(oci_client: Any) -> None:
    oci_client.list_objects.return_value = []
    oci_client.head_object.side_effect = not_found_error()
    await OciStorage().upload("d/a.txt", "body")
    oci_client.put_object.assert_awaited_once_with(BUCKET, "d/a.txt", b"body", "text/plain; charset=utf-8")


async def test_upload_bytes_passes_content_type_through(oci_client: Any) -> None:
    oci_client.list_objects.return_value = []
    await OciStorage().upload_bytes("a.png", b"\x89PNG", content_type="image/png")
    oci_client.put_object.assert_awaited_once_with(BUCKET, "a.png", b"\x89PNG", "image/png")


async def test_upload_bytes_without_content_type_sends_none(oci_client: Any) -> None:
    oci_client.list_objects.return_value = []
    await OciStorage().upload_bytes("a.bin", b"x")
    oci_client.put_object.assert_awaited_once_with(BUCKET, "a.bin", b"x", None)


async def test_upload_refuses_a_path_holding_objects(oci_client: Any) -> None:
    oci_client.list_objects.return_value = ["d/x.txt", "d/y.txt"]
    with pytest.raises(StoragePathConflictError) as excinfo:
        await OciStorage().upload("d", "body")
    assert excinfo.value.path == "d"
    assert excinfo.value.conflicts == ["d/x.txt", "d/y.txt"]
    oci_client.put_object.assert_not_awaited()


async def test_upload_refuses_a_path_nested_under_a_stored_object(oci_client: Any) -> None:
    oci_client.list_objects.return_value = []
    oci_client.head_object.return_value = "text/plain"
    with pytest.raises(StoragePathConflictError) as excinfo:
        await OciStorage().upload("a/b/c.txt", "body")
    # The first ancestor that exists is reported; "a" is probed before "a/b".
    assert excinfo.value.conflicts == ["a"]
    oci_client.put_object.assert_not_awaited()


async def test_upload_conflict_probe_propagates_other_service_errors(oci_client: Any) -> None:
    from oci.exceptions import ServiceError

    oci_client.list_objects.return_value = []
    oci_client.head_object.side_effect = forbidden_error()
    with pytest.raises(ServiceError):
        await OciStorage().upload("a/b.txt", "body")


async def test_delete_removes_the_object(oci_client: Any) -> None:
    await OciStorage().delete("a.txt")
    oci_client.delete_object.assert_awaited_once_with(BUCKET, "a.txt")


async def test_delete_maps_missing_object_to_file_not_found(oci_client: Any) -> None:
    oci_client.delete_object.side_effect = not_found_error()
    with pytest.raises(FileNotFoundError, match="gone.txt"):
        await OciStorage().delete("gone.txt")


async def test_delete_propagates_other_service_errors(oci_client: Any) -> None:
    from oci.exceptions import ServiceError

    oci_client.delete_object.side_effect = forbidden_error()
    with pytest.raises(ServiceError):
        await OciStorage().delete("denied.txt")


@pytest.mark.parametrize("path", ["", ".", "/", "  ", "a/..", "./x/../.."])
async def test_delete_dir_refuses_the_store_root(path: str, oci_client: Any) -> None:
    with pytest.raises(ValueError, match="storage root"):
        await OciStorage().delete_dir(path)
    oci_client.list_objects.assert_not_awaited()


async def test_delete_dir_deletes_each_object_under_the_prefix(oci_client: Any) -> None:
    oci_client.list_objects.return_value = ["d/a.txt", "d/b.txt"]
    await OciStorage().delete_dir("d")
    oci_client.list_objects.assert_awaited_once_with(BUCKET, prefix="d/")
    assert [c.args for c in oci_client.delete_object.await_args_list] == [
        (BUCKET, "d/a.txt"),
        (BUCKET, "d/b.txt"),
    ]


async def test_delete_dir_keeps_a_trailing_slash_once(oci_client: Any) -> None:
    oci_client.list_objects.return_value = ["d/a.txt"]
    await OciStorage().delete_dir("d/")
    oci_client.list_objects.assert_awaited_once_with(BUCKET, prefix="d/")


async def test_delete_dir_on_an_empty_prefix_is_file_not_found(oci_client: Any) -> None:
    oci_client.list_objects.return_value = []
    with pytest.raises(FileNotFoundError, match="not found or empty"):
        await OciStorage().delete_dir("d")
    oci_client.delete_object.assert_not_awaited()


async def test_stat_reports_the_stored_content_type(oci_client: Any) -> None:
    oci_client.head_object.return_value = "image/png"
    assert await OciStorage().stat("a.png") == ObjectStat(content_type="image/png")


async def test_stat_maps_missing_object_to_file_not_found(oci_client: Any) -> None:
    oci_client.head_object.side_effect = not_found_error()
    with pytest.raises(FileNotFoundError, match="nope.png"):
        await OciStorage().stat("nope.png")


async def test_stat_propagates_other_service_errors(oci_client: Any) -> None:
    from oci.exceptions import ServiceError

    oci_client.head_object.side_effect = forbidden_error()
    with pytest.raises(ServiceError):
        await OciStorage().stat("denied.png")
