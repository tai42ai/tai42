"""The platform's holder kinds: each one's feature tag is a tag the platform's own routes carry."""

from __future__ import annotations

from tai42_skeleton.app.route_registry import load_all_routes
from tai42_skeleton.tools import holder_kinds
from tai42_skeleton.tools.holder_kinds import HolderKind

_KINDS = [getattr(holder_kinds, name) for name in holder_kinds.__all__ if name != "HolderKind"]


def test_every_kind_is_a_holder_kind_with_a_distinct_kind_name() -> None:
    assert all(isinstance(kind, HolderKind) for kind in _KINDS)
    assert len({kind.kind for kind in _KINDS}) == len(_KINDS)


def test_every_kind_tag_is_a_tag_a_registered_route_carries() -> None:
    route_tags = {tag for meta in load_all_routes() for tag in meta.tags}
    assert {kind.kind: kind.tag for kind in _KINDS if kind.tag not in route_tags} == {}


def test_a_kind_builds_a_holder_under_its_own_kind_and_tag() -> None:
    holder = holder_kinds.HOOK.holder(name="nightly", summary="hook 'nightly'", locator={"site": "condition"})
    assert holder.model_dump() == {
        "kind": "hook",
        "tag": "hooks",
        "name": "nightly",
        "locator": {"site": "condition"},
        "summary": "hook 'nightly'",
    }
