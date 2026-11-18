"""Store writes, through their resource manager: an upload evicts the stale compiled entry it
replaced; ``delete_template`` and ``delete_template_dir`` delegate to storage and evict exactly the
compiled entries they remove, even when a directory delete fails part-way; a delete of an object a
stored object names raises the template layer's typed refusal and deletes nothing. All storage is an
in-memory ``Storage`` double."""

from __future__ import annotations

import pytest
from tai42_contract.errors import OperationError
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.template import HolderUnreadableError, PartialDirectoryDeleteError, StoredObjectHeldError
from tai42_skeleton.template import resource_manager as rm_mod
from tai42_skeleton.template.settings import TemplateCacheSettings

from ._manager_fixtures import _manager

# --- upload invalidates the compiled-template cache -------------------------


async def test_upload_template_evicts_stale_compiled_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-uploading to the same id must not keep serving the old compiled body:
    the single key is evicted on upload, so the next render reflects new content
    (cache enabled)."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"m.j2": "v1 {{ x }}"})
    assert await manager.render_by_id("m.j2", {"x": 1}) == "v1 1"  # populate cache

    await manager.upload_template("m.j2", "v2 {{ x }}")
    # Uploading evicts the compiled entry, so the next render reflects v2.
    assert await manager.render_by_id("m.j2", {"x": 1}) == "v2 1"


# --- delete invalidates the compiled-template cache (targeted) --------------


async def test_delete_template_delegates_and_evicts_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"d.j2": "{{ x }}"})
    await manager.render_by_id("d.j2", {"x": 1})  # populate cache
    populated = manager.get_cache_info()
    assert populated is not None
    assert populated.currsize == 1

    await manager.delete_template("d.j2")
    assert store.deleted == ["d.j2"]
    evicted = manager.get_cache_info()
    assert evicted is not None
    assert evicted.currsize == 0


async def test_delete_template_evicts_only_deleted_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting one template evicts only that compiled entry; others stay cached
    (no whole-cache flush)."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"a.j2": "{{ x }}", "b.j2": "{{ y }}"})
    await manager.render_by_id("a.j2", {"x": 1})
    await manager.render_by_id("b.j2", {"y": 2})
    both = manager.get_cache_info()
    assert both is not None
    assert both.currsize == 2

    await manager.delete_template("a.j2")
    survivor = manager.get_cache_info()
    assert survivor is not None
    assert survivor.currsize == 1
    # The surviving entry is served from cache (no re-fetch needed).
    assert await manager.render_by_id("b.j2", {"y": 9}) == "9"


async def test_delete_template_dir_evicts_prefix_even_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dir delete is non-atomic: entries under the prefix must be evicted in the
    ``finally`` even when the provider's delete_dir raises."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"dir/a.j2": "{{ x }}"})
    await manager.render_by_id("dir/a.j2", {"x": 1})
    populated = manager.get_cache_info()
    assert populated is not None
    assert populated.currsize == 1

    async def boom(path: str) -> None:
        raise RuntimeError("partial delete")

    monkeypatch.setattr(store, "delete_dir", boom)
    with pytest.raises(PartialDirectoryDeleteError, match="partial delete") as caught:
        await manager.delete_template_dir("dir")
    assert caught.value.path == "dir"
    assert isinstance(caught.value.__cause__, RuntimeError)
    evicted = manager.get_cache_info()
    assert evicted is not None
    assert evicted.currsize == 0


@pytest.mark.parametrize("fault", [FileNotFoundError("dir"), ValueError("dir is the storage root")])
async def test_delete_template_dir_passes_the_providers_pre_deletion_error_through(
    monkeypatch: pytest.MonkeyPatch, fault: Exception
) -> None:
    """The contract's pre-deletion errors from ``delete_dir`` propagate as they are; the
    eviction still runs."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, store = _manager({"dir/a.j2": "{{ x }}"})
    await manager.render_by_id("dir/a.j2", {"x": 1})

    async def refuse(path: str) -> None:
        raise fault

    monkeypatch.setattr(store, "delete_dir", refuse)
    with pytest.raises(type(fault)) as caught:
        await manager.delete_template_dir("dir")
    assert caught.value is fault
    evicted = manager.get_cache_info()
    assert evicted is not None
    assert evicted.currsize == 0


async def test_delete_template_dir_evicts_only_under_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dir delete evicts only compiled templates under that prefix; templates
    outside the dir stay cached."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager, _ = _manager({"dir/a.j2": "{{ x }}", "other.j2": "{{ y }}"})
    await manager.render_by_id("dir/a.j2", {"x": 1})
    await manager.render_by_id("other.j2", {"y": 2})
    both = manager.get_cache_info()
    assert both is not None
    assert both.currsize == 2

    await manager.delete_template_dir("dir")
    survivor = manager.get_cache_info()
    assert survivor is not None
    assert survivor.currsize == 1
    assert await manager.render_by_id("other.j2", {"y": 7}) == "7"


async def test_delete_template_dir_success_delegates() -> None:
    manager, store = _manager({"dir/a.j2": "A", "dir/b.j2": "B"})
    await manager.delete_template_dir("dir")
    assert store.deleted_dirs == ["dir"]
    assert store.items == {}


async def test_delete_template_dir_with_caching_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """With caching off there is no compiled-template cache to invalidate; the
    dir delete still delegates and must not raise."""
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=0, max_size=8))
    manager, store = _manager({"dir/a.j2": "A"})
    assert manager.get_cache_info() is None  # caching disabled

    await manager.delete_template_dir("dir")
    assert store.deleted_dirs == ["dir"]
    assert store.items == {}


# --- a held object is refused with the template layer's typed errors --------

_HOLDER = ReferenceHolder(kind="hook", tag="hooks", name="h", summary="hook 'h' names 'dir/a.j2' at condition")


async def _holds_dir_a(ids: frozenset[str]) -> list[ReferenceHolder]:
    return [_HOLDER] if "dir/a.j2" in ids else []


async def _unreadable(ids: frozenset[str]) -> list[ReferenceHolder]:
    raise ValueError("bad value 'secret'")


class _RefereeOwnError(OperationError):
    status = 409


async def _answers_its_own_error(ids: frozenset[str]) -> list[ReferenceHolder]:
    raise _RefereeOwnError("a referee's own typed answer")


async def test_a_held_object_raises_the_held_error_and_deletes_nothing() -> None:
    manager, store = _manager({"dir/a.j2": "A"}, held_by=_holds_dir_a)
    with pytest.raises(StoredObjectHeldError) as caught:
        await manager.delete_object("dir/a.j2")
    assert caught.value.head == "stored template 'dir/a.j2' cannot be deleted"
    assert caught.value.holders == [_HOLDER]
    assert str(caught.value) == ("stored template 'dir/a.j2' cannot be deleted\nhook 'h' names 'dir/a.j2' at condition")
    assert store.deleted == []
    assert store.items == {"dir/a.j2": "A"}


async def test_a_held_directory_raises_the_held_error_and_deletes_nothing() -> None:
    manager, store = _manager({"dir/a.j2": "A", "dir/b.j2": "B"}, held_by=_holds_dir_a)
    with pytest.raises(StoredObjectHeldError) as caught:
        await manager.delete_template_dir("dir")
    assert caught.value.head == "directory 'dir' cannot be deleted"
    assert caught.value.holders == [_HOLDER]
    assert store.deleted_dirs == []
    assert set(store.items) == {"dir/a.j2", "dir/b.j2"}


async def test_an_unreadable_holder_raises_the_unreadable_error_chained_to_its_fault() -> None:
    manager, store = _manager({"dir/a.j2": "A"}, held_by=_unreadable)
    with pytest.raises(HolderUnreadableError) as caught:
        await manager.delete_object("dir/a.j2")
    assert str(caught.value) == "stored template 'dir/a.j2' cannot be deleted: a holder could not be read (ValueError)"
    assert isinstance(caught.value.__cause__, ValueError)
    assert store.deleted == []


async def test_a_referees_own_operation_error_passes_through_as_it_is() -> None:
    manager, store = _manager({"dir/a.j2": "A"}, held_by=_answers_its_own_error)
    with pytest.raises(_RefereeOwnError, match="a referee's own typed answer"):
        await manager.delete_object("dir/a.j2")
    assert store.deleted == []


async def test_a_referees_own_operation_error_on_a_directory_delete_passes_through_as_it_is() -> None:
    manager, store = _manager({"dir/a.j2": "A"}, held_by=_answers_its_own_error)
    with pytest.raises(_RefereeOwnError, match="a referee's own typed answer"):
        await manager.delete_template_dir("dir")
    assert store.deleted_dirs == []
