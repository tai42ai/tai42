"""Contract tests for the tool-metadata overlay surface.

Pin the folder + overlay record models (round-trip, tri-state ``hidden``,
defaults), the typed errors (they carry their identity), and the
``ToolMetaStore`` Protocol (``runtime_checkable``, full method set).
"""

from __future__ import annotations


def test_folder_record_round_trips():
    from tai42_contract.tool_meta import FolderRecord

    rec = FolderRecord(id="f1", name="Research", parent_id="root")
    again = FolderRecord(**rec.model_dump())
    assert again == rec
    assert again.parent_id == "root"


def test_folder_record_root_defaults_parent_none():
    from tai42_contract.tool_meta import FolderRecord

    rec = FolderRecord(id="f1", name="Root")
    assert rec.parent_id is None


def test_tool_meta_record_round_trips_and_hidden_is_tristate():
    from tai42_contract.tool_meta import ToolMetaRecord

    for hidden in (None, True, False):
        rec = ToolMetaRecord(
            tool_name="web_search",
            display_name="Web Search",
            folder_id="f1",
            tags=["search"],
            hidden=hidden,
        )
        again = ToolMetaRecord(**rec.model_dump())
        assert again == rec
        assert again.hidden is hidden


def test_tool_meta_record_defaults():
    from tai42_contract.tool_meta import ToolMetaRecord

    rec = ToolMetaRecord(tool_name="web_search")
    assert rec.display_name is None
    assert rec.folder_id is None
    assert rec.tags == []
    assert rec.hidden is None
    assert rec.badges == []


def test_tool_meta_record_badges_round_trip():
    from tai42_contract.tool_meta import ToolMetaRecord

    rec = ToolMetaRecord(tool_name="web_search", badges=["storage-read", "network", "llm"])
    again = ToolMetaRecord(**rec.model_dump())
    assert again == rec
    assert again.badges == ["storage-read", "network", "llm"]


def test_tool_meta_record_badges_accept_hyphen_and_reject_slash():
    import pytest
    from pydantic import ValidationError

    from tai42_contract.tool_meta import ToolMetaRecord

    # A hyphenated vocabulary term is the canonical badge form (TAG_RE allows it).
    assert ToolMetaRecord(tool_name="t", badges=["external-send"]).badges == ["external-send"]
    # TAG_RE forbids the slash, so a namespaced/pathy badge is refused loudly.
    for bad in ("storage/read", "Storage", "-leading", "has space"):
        with pytest.raises(ValidationError):
            ToolMetaRecord(tool_name="t", badges=[bad])


def test_tool_meta_errors_carry_identity():
    from tai42_contract.tool_meta import (
        FolderCycleError,
        FolderNameConflictError,
        FolderNotEmptyError,
        FolderNotFoundError,
        ToolMetaError,
    )

    for err_cls in (FolderNotFoundError, FolderNotEmptyError, FolderCycleError):
        err = err_cls("f1")
        assert isinstance(err, ToolMetaError)
        assert err.folder_id == "f1"

    conflict = FolderNameConflictError("Research", None)
    assert isinstance(conflict, ToolMetaError)
    assert conflict.name == "Research"
    assert conflict.parent_id is None


def test_tool_meta_store_is_runtime_checkable():
    from tai42_contract.tool_meta import ToolMetaStore

    class Conforms:
        async def create_folder(self, *a: object, **k: object): ...
        async def rename_folder(self, *a: object, **k: object): ...
        async def move_folder(self, *a: object, **k: object): ...
        async def delete_folder(self, *a: object, **k: object): ...
        async def list_folders(self, *a: object, **k: object): ...
        async def upsert_meta(self, *a: object, **k: object): ...
        async def merge_meta(self, *a: object, **k: object): ...
        async def get_meta(self, *a: object, **k: object): ...
        async def list_meta(self, *a: object, **k: object): ...
        async def delete_meta(self, *a: object, **k: object): ...
        async def rename_tool(self, *a: object, **k: object): ...

    class Missing:
        async def create_folder(self, *a: object, **k: object): ...

    assert isinstance(Conforms(), ToolMetaStore)
    assert not isinstance(Missing(), ToolMetaStore)
