"""Unit tests for the pooled client: the async facade and the auth modes.

The OCI SDK is mocked throughout — no principal is exchanged and no request is
sent. The facade's job is to run a synchronous SDK call on a worker thread and
map its response, so each test asserts the SDK call shape and the mapped result.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import oci
import pytest

from tai42_storage_oci.client import OciClient, OciObjectStorage, _config, _signer
from tai42_storage_oci.settings import OciSettings

NAMESPACE = "tenancy-ns"
BUCKET = "tenant-bucket"


def _page(names: list[str], next_start: str | None) -> Any:
    return SimpleNamespace(data=SimpleNamespace(objects=[SimpleNamespace(name=n) for n in names], next_start_with=next_start))


def _facade(sdk: Any) -> OciObjectStorage:
    return OciObjectStorage(sdk, NAMESPACE)


async def test_get_object_returns_bytes_and_content_type() -> None:
    sdk = MagicMock()
    sdk.get_object.return_value = SimpleNamespace(
        data=SimpleNamespace(content=b"payload"), headers={"content-type": "image/png"}
    )
    assert await _facade(sdk).get_object(BUCKET, "a.png") == (b"payload", "image/png")
    sdk.get_object.assert_called_once_with(NAMESPACE, BUCKET, "a.png")


async def test_put_object_sends_content_type_when_given() -> None:
    sdk = MagicMock()
    await _facade(sdk).put_object(BUCKET, "a.png", b"x", "image/png")
    sdk.put_object.assert_called_once_with(NAMESPACE, BUCKET, "a.png", b"x", content_type="image/png")


async def test_put_object_omits_content_type_when_absent() -> None:
    sdk = MagicMock()
    await _facade(sdk).put_object(BUCKET, "a.bin", b"x", None)
    sdk.put_object.assert_called_once_with(NAMESPACE, BUCKET, "a.bin", b"x")


async def test_list_objects_follows_the_paging_cursor() -> None:
    sdk = MagicMock()
    sdk.list_objects.side_effect = [_page(["a"], "a"), _page(["b"], None)]
    assert await _facade(sdk).list_objects(BUCKET) == ["a", "b"]
    first, second = sdk.list_objects.call_args_list
    assert first.kwargs == {"fields": "name"}
    assert second.kwargs == {"fields": "name", "start": "a"}


async def test_list_objects_passes_the_prefix() -> None:
    sdk = MagicMock()
    sdk.list_objects.side_effect = [_page([], None)]
    assert await _facade(sdk).list_objects(BUCKET, prefix="d/") == []
    assert sdk.list_objects.call_args.kwargs == {"fields": "name", "prefix": "d/"}


async def test_head_object_returns_the_content_type() -> None:
    sdk = MagicMock()
    sdk.head_object.return_value = SimpleNamespace(headers={"content-type": "text/plain"})
    assert await _facade(sdk).head_object(BUCKET, "a.txt") == "text/plain"


async def test_head_object_without_a_content_type_header_is_none() -> None:
    sdk = MagicMock()
    sdk.head_object.return_value = SimpleNamespace(headers={})
    assert await _facade(sdk).head_object(BUCKET, "a.txt") is None


async def test_delete_object_calls_the_sdk() -> None:
    sdk = MagicMock()
    await _facade(sdk).delete_object(BUCKET, "a.txt")
    sdk.delete_object.assert_called_once_with(NAMESPACE, BUCKET, "a.txt")


def test_workload_identity_is_the_signer_default(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    captured: dict[str, Any] = {}

    def fake(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(oci.auth.signers, "get_oke_workload_identity_resource_principal_signer", fake)
    assert _signer(OciSettings(auth="workload_identity")) is sentinel
    # Unset token path leaves the SDK's own default.
    assert captured == {}


def test_workload_identity_forwards_a_set_token_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        oci.auth.signers,
        "get_oke_workload_identity_resource_principal_signer",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    _signer(OciSettings(auth="workload_identity", service_account_token_path="/var/run/token"))
    assert captured == {"service_account_token_path": "/var/run/token"}


def test_instance_principal_mode_builds_its_own_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(oci.auth.signers, "InstancePrincipalsSecurityTokenSigner", lambda: sentinel)
    assert _signer(OciSettings(auth="instance_principal")) is sentinel


def test_config_file_mode_uses_no_signer() -> None:
    assert _signer(OciSettings(auth="config_file")) is None


def test_principal_modes_pass_an_empty_config() -> None:
    assert _config(OciSettings(auth="workload_identity")) == {}


def test_config_file_mode_reads_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oci.config, "from_file", lambda: {"tenancy": "ocid1.tenancy"})
    assert _config(OciSettings(auth="config_file")) == {"tenancy": "ocid1.tenancy"}


def test_a_set_region_overrides_the_config() -> None:
    assert _config(OciSettings(auth="workload_identity", region="us-ashburn-1"))["region"] == "us-ashburn-1"


async def test_create_builds_a_client_with_the_settings_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_storage_oci import settings as settings_module

    monkeypatch.setenv("STORAGE_OCI_NAMESPACE", NAMESPACE)
    monkeypatch.setenv("STORAGE_OCI_CONNECT_TIMEOUT", "3")
    monkeypatch.setenv("STORAGE_OCI_READ_TIMEOUT", "11")
    settings_module.oci_settings.cache_clear()
    monkeypatch.setattr(oci.auth.signers, "get_oke_workload_identity_resource_principal_signer", lambda **_: "signer")
    captured: dict[str, Any] = {}

    def fake_client(config: Any, **kwargs: Any) -> Any:
        captured["config"] = config
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(oci.object_storage, "ObjectStorageClient", fake_client)
    facade = await OciClient()._create()
    assert isinstance(facade, OciObjectStorage)
    assert captured["kwargs"] == {"timeout": (3, 11), "signer": "signer"}
    settings_module.oci_settings.cache_clear()


async def test_create_resolves_the_namespace_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_storage_oci import settings as settings_module

    monkeypatch.delenv("STORAGE_OCI_NAMESPACE", raising=False)
    settings_module.oci_settings.cache_clear()
    monkeypatch.setattr(oci.auth.signers, "get_oke_workload_identity_resource_principal_signer", lambda **_: "signer")
    sdk = MagicMock()
    sdk.get_namespace.return_value = SimpleNamespace(data="resolved-ns")
    monkeypatch.setattr(oci.object_storage, "ObjectStorageClient", lambda config, **kwargs: sdk)
    await OciClient()._create()
    sdk.get_namespace.assert_called_once_with()
    settings_module.oci_settings.cache_clear()


async def test_create_rejects_connection_kwargs() -> None:
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        await OciClient()._create(region="us-ashburn-1")


async def test_close_owns_nothing() -> None:
    assert await OciClient()._close(_facade(MagicMock())) is None


def test_only_a_transport_failure_evicts_the_pooled_client() -> None:
    evicting = OciClient()._disconnection_exceptions()
    assert evicting == (oci.exceptions.RequestException,)
    assert not issubclass(oci.exceptions.ServiceError, evicting)
