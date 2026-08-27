"""Contract tests for the generic versioned-document store + the preset view.

Pin the typed versioning and preset surface: the record/body models round-trip,
the ``VersionedStore`` and ``PresetStore`` Protocols are
``runtime_checkable``, the typed errors carry their identity, and the
carry-forward sentinel is expressed at the signature level. Behavioral
carry-forward lives skeleton-side; here we pin only the contract-level guarantees.
"""

from __future__ import annotations

import inspect

import pydantic
import pytest

# -- Generic versioned-document models -----------------------------------------


def test_document_record_round_trips():
    from tai42_contract.versioning import DocumentRecord

    rec = DocumentRecord(kind="preset", name="summarize", active_version=3, created_at="2026-07-06T00:00:00Z")
    assert rec.is_active is True  # default
    again = DocumentRecord(**rec.model_dump())
    assert again == rec
    assert again.active_version == 3


def test_document_version_round_trips_with_tags():
    from tai42_contract.versioning import DocumentVersion

    ver = DocumentVersion(
        version=2,
        body={"base_tool": "web", "fixed_kwargs": {"k": 1}},
        tags=["release", "stable"],
        created_at="2026-07-06T00:00:00Z",
        is_current=True,
    )
    again = DocumentVersion(**ver.model_dump())
    assert again == ver
    assert again.tags == ["release", "stable"]
    assert again.is_current is True


def test_document_version_tags_default_empty_and_is_current_defaults_false():
    from tai42_contract.versioning import DocumentVersion

    ver = DocumentVersion(version=1, body={}, created_at="2026-07-06T00:00:00Z")
    assert ver.tags == []
    assert ver.is_current is False


def test_document_version_is_immutable():
    from tai42_contract.versioning import DocumentVersion

    ver = DocumentVersion(version=1, body={}, created_at="2026-07-06T00:00:00Z")
    with pytest.raises(pydantic.ValidationError):
        ver.version = 9  # frozen — a written version never changes


# -- Generic versioned-document errors -----------------------------------------


def test_versioning_errors_carry_kind_and_name():
    from tai42_contract.versioning import (
        DocumentExistsError,
        DocumentNotFoundError,
        DocumentStoreError,
        DocumentVersionNotFoundError,
    )

    for err_cls in (DocumentExistsError, DocumentNotFoundError, DocumentVersionNotFoundError):
        err = err_cls("preset", "summarize")
        assert isinstance(err, DocumentStoreError)
        assert err.kind == "preset"
        assert err.name == "summarize"


def test_document_version_not_found_carries_version():
    from tai42_contract.versioning import DocumentVersionNotFoundError

    err = DocumentVersionNotFoundError("preset", "summarize", 7)
    assert err.version == 7
    assert "7" in str(err)
    # version is optional so the (kind, name) construction stays uniform.
    assert DocumentVersionNotFoundError("preset", "summarize").version is None


# -- VersionedStore Protocol ---------------------------------------------------


def test_versioned_store_is_runtime_checkable():
    from tai42_contract.versioning import VersionedStore

    class Conforms:
        def transaction(self, *a: object, **k: object): ...
        async def create(self, *a: object, **k: object): ...
        async def save_version(self, *a: object, **k: object): ...
        async def list(self, *a: object, **k: object): ...
        async def get(self, *a: object, **k: object): ...
        async def get_active_body(self, *a: object, **k: object): ...
        async def list_versions(self, *a: object, **k: object): ...
        async def get_version(self, *a: object, **k: object): ...
        async def rollback(self, *a: object, **k: object): ...
        async def soft_delete(self, *a: object, **k: object): ...
        async def delete(self, *a: object, **k: object): ...
        async def rename(self, *a: object, **k: object): ...

    class Missing:
        async def create(self, *a: object, **k: object): ...

    assert isinstance(Conforms(), VersionedStore)
    assert not isinstance(Missing(), VersionedStore)


# -- Preset body model ---------------------------------------------------------


def test_preset_body_round_trips():
    from tai42_contract.presets import PresetBody

    body = PresetBody(
        base_tool="web_search",
        description="canned search",
        fixed_kwargs={"engine": "brave"},
        extensions=[["chain"], ["batch"]],
    )
    again = PresetBody(**body.model_dump())
    assert again == body
    assert again.extensions == [["chain"], ["batch"]]


def test_preset_body_round_trips_author_bound_config():
    # A preset over an ``ask_external`` tool binds its verifier as author config on
    # the combo element; that ``{"name", "config"}`` mapping must survive a body
    # dump/reload round-trip unchanged (else the bound verifier vanishes on reload).
    from tai42_contract.manifest import ExtensionElement
    from tai42_contract.presets import PresetBody

    combo: list[ExtensionElement] = [
        {"name": "ask_external", "config": {"verifier": {"name": "github", "config": {"secret_env": "GH"}}}}
    ]
    body = PresetBody(base_tool="sign", extensions=[combo])
    again = PresetBody(**body.model_dump())
    assert again == body
    assert again.extensions == [combo]


def test_preset_body_defaults():
    from tai42_contract.presets import PresetBody

    body = PresetBody(base_tool="web_search")
    assert body.description == ""
    assert body.fixed_kwargs == {}
    assert body.extensions == []
    assert body.output_schema is None
    assert body.input_schema is None


def test_preset_body_round_trips_input_schema():
    # input_schema must survive a body dump/reload like output_schema — dropping it
    # would silently un-enforce the structured input on reload.
    from tai42_contract.presets import PresetBody

    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    body = PresetBody(base_tool="sandbox_exec", input_schema=schema)
    again = PresetBody(**body.model_dump())
    assert again == body
    assert again.input_schema == schema


def test_preset_input_schema_support_carries_payload_arg():
    from tai42_contract.presets import PresetInputSchemaSupport

    support = PresetInputSchemaSupport(payload_arg="argv")
    assert support.payload_arg == "argv"
    assert PresetInputSchemaSupport(**support.model_dump()) == support


def test_preset_store_create_preset_threads_input_schema():
    # create_preset carries the optional input_schema alongside output_schema, both
    # defaulting to None (no schema declared).
    from tai42_contract.presets import PresetStore

    sig = inspect.signature(PresetStore.create_preset)
    assert sig.parameters["input_schema"].default is None
    assert sig.parameters["output_schema"].default is None


# -- Preset errors -------------------------------------------------------------


def test_preset_errors_carry_name():
    from tai42_contract.presets import (
        PresetError,
        PresetExistsError,
        PresetNameConflictError,
        PresetNotFoundError,
        PresetVersionNotFoundError,
    )

    for err_cls in (PresetNotFoundError, PresetExistsError, PresetNameConflictError):
        err = err_cls("summarize")
        assert isinstance(err, PresetError)
        assert err.name == "summarize"

    ver_err = PresetVersionNotFoundError("summarize", 4)
    assert ver_err.name == "summarize"
    assert ver_err.version == 4


# -- PresetStore Protocol ------------------------------------------------------


def test_preset_store_is_runtime_checkable():
    from tai42_contract.presets import PresetStore

    class Conforms:
        async def create_preset(self, *a: object, **k: object): ...
        async def save_version(self, *a: object, **k: object): ...
        async def list_presets(self, *a: object, **k: object): ...
        async def get_preset(self, *a: object, **k: object): ...
        async def get_active_kwargs(self, *a: object, **k: object): ...
        async def list_versions(self, *a: object, **k: object): ...
        async def get_version(self, *a: object, **k: object): ...
        async def get_active_body(self, *a: object, **k: object): ...
        async def rollback(self, *a: object, **k: object): ...
        async def soft_delete(self, *a: object, **k: object): ...
        async def rename_preset(self, *a: object, **k: object): ...

    class Missing:
        async def create_preset(self, *a: object, **k: object): ...

    assert isinstance(Conforms(), PresetStore)
    assert not isinstance(Missing(), PresetStore)


def test_preset_store_save_version_editable_fields_carry_forward_by_default():
    # The carry-forward sentinel expressed at the signature level: fixed_kwargs /
    # extensions default to None (= carry the active value forward), while
    # output_schema / input_schema use the distinct CARRY_FORWARD sentinel (their
    # cleared state is None, so None cannot double as "not provided"). description is
    # now an editable per-version field defaulting to None (= carry forward, explicit
    # string sets it). base_tool is never an argument — it always carries. input_schema
    # is keyword-only and placed AFTER description so description keeps its original
    # positional slot, keeping the surface positionally back-compatible. tags is a
    # keyword-only, default-None addition that labels the new version in the same save
    # commit; None leaves the version untagged.
    from tai42_contract.presets import CARRY_FORWARD, PresetStore

    sig = inspect.signature(PresetStore.save_version)
    assert list(sig.parameters) == [
        "self",
        "name",
        "fixed_kwargs",
        "extensions",
        "output_schema",
        "description",
        "input_schema",
        "tags",
    ]
    for field in ("fixed_kwargs", "extensions"):
        assert sig.parameters[field].default is None, f"{field} must default to the carry-forward sentinel"
    assert sig.parameters["output_schema"].default is CARRY_FORWARD
    assert sig.parameters["input_schema"].default is CARRY_FORWARD
    assert sig.parameters["input_schema"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["description"].default is None
    assert sig.parameters["tags"].default is None
    assert sig.parameters["tags"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "base_tool" not in sig.parameters


def test_preset_spec_is_reexported():
    # PresetSpec keeps its single home in agent.base and its re-export here.
    from tai42_contract.agent.base import PresetSpec as SpecHome
    from tai42_contract.presets import PresetSpec

    assert PresetSpec is SpecHome
