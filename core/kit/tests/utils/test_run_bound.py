"""The bounded-run marker: set only inside ``bounded_run()``, reset when it ends."""

from __future__ import annotations

import pytest

from tai42_kit.utils.run_bound import bounded_run, in_bounded_run


def test_a_run_is_unbounded_outside_the_scope() -> None:
    assert in_bounded_run() is False


def test_the_scope_marks_the_run_bounded_and_resets_it() -> None:
    with bounded_run():
        assert in_bounded_run() is True
    assert in_bounded_run() is False


def test_a_nested_scope_keeps_the_outer_mark() -> None:
    with bounded_run():
        with bounded_run():
            assert in_bounded_run() is True
        assert in_bounded_run() is True
    assert in_bounded_run() is False


def test_the_mark_is_reset_when_the_run_raises() -> None:
    with pytest.raises(RuntimeError), bounded_run():
        raise RuntimeError("boom")
    assert in_bounded_run() is False
