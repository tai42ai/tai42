"""The kit staged-generation primitives, proven on a registry of a made-up kind."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from tai42_kit.registry import NamedFactoryRegistry, StagedGeneration, StagedSlot, same_factory

# -- StagedGeneration ---------------------------------------------------------


def test_generation_writes_committed_when_not_staging() -> None:
    gen: StagedGeneration[dict[str, int]] = StagedGeneration(dict)
    assert gen.staging is False
    gen.write_target()["a"] = 1
    assert gen.committed() == {"a": 1}


def test_generation_stages_off_to_the_side_until_commit() -> None:
    gen: StagedGeneration[dict[str, int]] = StagedGeneration(dict)
    gen.write_target()["live"] = 1
    gen.begin()
    assert gen.staging is True
    assert gen.write_target() == {}
    gen.write_target()["next"] = 2
    assert gen.committed() == {"live": 1}
    staged = gen.write_target()
    gen.commit()
    assert gen.staging is False
    assert gen.committed() is staged
    assert gen.committed() == {"next": 2}


def test_generation_commit_without_begin_is_a_no_op() -> None:
    gen: StagedGeneration[list[str]] = StagedGeneration(list)
    gen.write_target().append("kept")
    committed = gen.committed()
    gen.commit()
    assert gen.committed() is committed
    assert gen.committed() == ["kept"]


def test_generation_abort_keeps_committed() -> None:
    gen: StagedGeneration[dict[str, int]] = StagedGeneration(dict)
    gen.write_target()["live"] = 1
    gen.begin()
    gen.write_target()["dropped"] = 2
    gen.abort()
    assert gen.staging is False
    assert gen.committed() == {"live": 1}
    assert gen.write_target() == {"live": 1}


def test_generation_abort_without_begin_is_a_no_op() -> None:
    gen: StagedGeneration[dict[str, int]] = StagedGeneration(dict)
    gen.write_target()["live"] = 1
    gen.abort()
    assert gen.committed() == {"live": 1}


def test_generation_begin_opens_a_fresh_empty_each_time() -> None:
    gen: StagedGeneration[dict[str, int]] = StagedGeneration(dict)
    gen.begin()
    gen.write_target()["first"] = 1
    gen.begin()
    assert gen.write_target() == {}


# -- StagedSlot ----------------------------------------------------------------


class _Gadget:
    def __init__(self, name: str) -> None:
        self.name = name


def _recording_slot() -> tuple[StagedSlot[_Gadget], list[str]]:
    replaced: list[str] = []
    return StagedSlot(on_replace=lambda old: replaced.append(old.name)), replaced


def test_slot_set_outside_staging_replaces_live_and_reports_the_old_value() -> None:
    slot, replaced = _recording_slot()
    assert slot.current() is None
    slot.set(_Gadget("one"))
    assert replaced == []
    slot.set(_Gadget("two"))
    assert replaced == ["one"]
    current = slot.current()
    assert current is not None
    assert current.name == "two"


def test_slot_keeps_live_when_nothing_staged() -> None:
    slot, replaced = _recording_slot()
    live = _Gadget("live")
    slot.set(live)
    slot.begin()
    assert slot.staged_or_current() is live
    slot.commit()
    assert slot.current() is live
    assert replaced == []


def test_slot_on_replace_runs_once_at_commit() -> None:
    slot, replaced = _recording_slot()
    slot.set(_Gadget("live"))
    slot.begin()
    staged = _Gadget("staged")
    slot.set(staged)
    assert replaced == []
    current = slot.current()
    assert current is not None
    assert current.name == "live"
    assert slot.staged_or_current() is staged
    slot.commit()
    assert replaced == ["live"]
    assert slot.current() is staged
    slot.commit()
    assert replaced == ["live"]


def test_slot_abort_drops_the_staged_value() -> None:
    slot, replaced = _recording_slot()
    live = _Gadget("live")
    slot.set(live)
    slot.begin()
    slot.set(_Gadget("staged"))
    slot.abort()
    assert slot.current() is live
    assert slot.staged_or_current() is live
    assert replaced == []
    # After abort the slot is no longer staging: a set replaces live.
    slot.set(_Gadget("after"))
    assert replaced == ["live"]


def test_slot_commit_into_an_empty_slot_reports_nothing() -> None:
    slot, replaced = _recording_slot()
    slot.begin()
    slot.set(_Gadget("first"))
    slot.commit()
    assert replaced == []
    current = slot.current()
    assert current is not None
    assert current.name == "first"


def test_slot_without_on_replace() -> None:
    slot: StagedSlot[int] = StagedSlot()
    slot.set(1)
    slot.set(2)
    slot.begin()
    slot.set(3)
    slot.commit()
    assert slot.current() == 3


def test_slot_reset_empties_without_reporting() -> None:
    slot, replaced = _recording_slot()
    slot.set(_Gadget("live"))
    slot.begin()
    slot.set(_Gadget("staged"))
    slot.reset()
    assert slot.current() is None
    assert slot.staged_or_current() is None
    assert replaced == []
    slot.set(_Gadget("fresh"))
    assert replaced == []


# -- same_factory -------------------------------------------------------------


def _fresh_class() -> type:
    class Widget:
        pass

    return Widget


def test_same_factory_identity() -> None:
    marker = _fresh_class()
    assert same_factory(marker, marker) is True


def test_same_factory_fresh_class_object_same_qualname_matches() -> None:
    first = _fresh_class()
    second = _fresh_class()
    assert first is not second
    assert same_factory(first, second) is True


def test_same_factory_lambdas_never_match_across_objects() -> None:
    def make() -> Callable[[], int]:
        return lambda: 1

    assert same_factory(make(), make()) is False


def test_same_factory_different_qualname_differs() -> None:
    class Alpha:
        pass

    class Beta:
        pass

    assert same_factory(Alpha, Beta) is False


def test_same_factory_without_qualname_differs() -> None:
    assert same_factory(object(), object()) is False


def test_same_factory_same_qualname_different_module_differs() -> None:
    first = _fresh_class()
    second = _fresh_class()
    second.__module__ = "elsewhere"
    assert same_factory(first, second) is False


# -- NamedFactoryRegistry -----------------------------------------------------


@pytest.fixture
def gizmos() -> NamedFactoryRegistry[Callable[[], object]]:
    return NamedFactoryRegistry("Gizmo")


def test_named_register_and_get(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    factory = _fresh_class()
    gizmos.register("g1", factory)
    assert gizmos.get("g1") is factory
    assert gizmos.get_staged("g1") is factory
    assert gizmos.items() == [("g1", factory)]


def test_named_unknown_name_raises_keyerror(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    with pytest.raises(KeyError, match="Unknown gizmo: 'missing'"):
        gizmos.get("missing")
    with pytest.raises(KeyError, match="Unknown gizmo: 'missing'"):
        gizmos.get_staged("missing")


def test_named_reregistering_fresh_class_same_qualname_is_a_no_op(
    gizmos: NamedFactoryRegistry[Callable[[], object]], caplog: pytest.LogCaptureFixture
) -> None:
    first = _fresh_class()
    gizmos.register("g1", first)
    with caplog.at_level(logging.DEBUG, logger="tai42_kit.registry.staged"):
        gizmos.register("g1", _fresh_class())
    assert gizmos.get("g1") is first
    assert any("re-registered" in record.getMessage() for record in caplog.records)


def test_named_lambda_under_held_name_raises(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    gizmos.register("g1", lambda: 1)
    with pytest.raises(ValueError, match="Gizmo 'g1' already registered"):
        gizmos.register("g1", lambda: 2)


def test_named_different_qualname_under_held_name_raises(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    class Alpha:
        pass

    class Beta:
        pass

    gizmos.register("g1", Alpha)
    with pytest.raises(ValueError, match="Gizmo 'g1' already registered"):
        gizmos.register("g1", Beta)


def test_named_items_are_name_sorted_fresh_lists(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    gizmos.register("b", _fresh_class())
    gizmos.register("a", lambda: 1)
    items = gizmos.items()
    assert [name for name, _ in items] == ["a", "b"]
    items.clear()
    assert len(gizmos.items()) == 2


def test_named_staging_isolates_the_committed_generation(
    gizmos: NamedFactoryRegistry[Callable[[], object]],
) -> None:
    live = _fresh_class()
    gizmos.register("live", live)
    gizmos.begin_staging()
    assert gizmos.names_staged() == []
    nxt = lambda: 2  # noqa: E731
    gizmos.register("next", nxt)
    assert gizmos.names_staged() == ["next"]
    assert gizmos.items_staged() == [("next", nxt)]
    assert gizmos.get_staged("next") is nxt
    with pytest.raises(KeyError):
        gizmos.get("next")
    assert gizmos.get("live") is live
    gizmos.commit_staging()
    assert gizmos.get("next") is nxt
    with pytest.raises(KeyError):
        gizmos.get("live")


def test_named_abort_drops_the_staged_generation(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    live = _fresh_class()
    gizmos.register("live", live)
    gizmos.begin_staging()
    gizmos.register("dropped", lambda: 0)
    gizmos.abort_staging()
    assert gizmos.items() == [("live", live)]
    assert gizmos.names_staged() == ["live"]


def test_named_abort_and_commit_without_begin_are_no_ops(
    gizmos: NamedFactoryRegistry[Callable[[], object]],
) -> None:
    live = _fresh_class()
    gizmos.register("live", live)
    gizmos.abort_staging()
    gizmos.commit_staging()
    assert gizmos.items() == [("live", live)]


def test_named_reset_clears_the_write_target(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    live = _fresh_class()
    gizmos.register("live", live)
    gizmos.begin_staging()
    gizmos.register("staged", lambda: 0)
    gizmos.reset()
    assert gizmos.names_staged() == []
    assert gizmos.items() == [("live", live)]
    gizmos.abort_staging()
    gizmos.reset()
    assert gizmos.items() == []


def test_named_before_add_runs_only_for_a_new_name(gizmos: NamedFactoryRegistry[Callable[[], object]]) -> None:
    calls: list[str] = []
    factory = _fresh_class()
    gizmos.register("g1", factory, before_add=lambda: calls.append("g1"))
    gizmos.register("g1", _fresh_class(), before_add=lambda: calls.append("again"))
    assert calls == ["g1"]
    with pytest.raises(ValueError, match="already registered"):
        gizmos.register("g1", lambda: 0, before_add=lambda: calls.append("collision"))
    assert calls == ["g1"]


def test_named_before_add_raise_leaves_the_registry_untouched(
    gizmos: NamedFactoryRegistry[Callable[[], object]],
) -> None:
    def refuse() -> None:
        raise ValueError("twin refused")

    with pytest.raises(ValueError, match="twin refused"):
        gizmos.register("g1", lambda: 0, before_add=refuse)
    assert gizmos.items() == []
