"""The one scope-coverage rule: public-only id sets, and every protected id covered (or the universal scope)."""

from __future__ import annotations

import pytest

from tai42_skeleton.access_control.coverage import is_public_only, scopes_cover

PUBLIC = "public"


@pytest.mark.parametrize(
    ("ids", "public_only"),
    [
        ([PUBLIC], True),
        ([PUBLIC, PUBLIC], True),
        ([], False),
        (["a"], False),
        ([PUBLIC, "a"], False),
    ],
)
def test_public_only_is_the_public_id_alone(ids: list[str], public_only: bool) -> None:
    assert is_public_only(ids, PUBLIC) is public_only


@pytest.mark.parametrize(
    ("ids", "scopes", "covered"),
    [
        (["a"], ["a"], True),
        (["a", "b"], ["a"], False),
        (["a", "b"], ["b", "a"], True),
        (["a", PUBLIC], ["a"], True),
        ([PUBLIC], [], True),
        (["a"], ["*"], True),
        (["a", "b"], ["*"], True),
        (["a"], [], False),
        ([], [], True),
    ],
)
def test_every_protected_id_must_be_covered(ids: list[str], scopes: list[str], covered: bool) -> None:
    assert scopes_cover(ids, scopes, PUBLIC) is covered
