"""The input-program order: dependency-first, smallest ready name first, a cycle appended sorted.

One tokenising pass per body finds every ``tjq_<name>`` sibling call; the order equals the
step-by-step selection it replaces (checked against that selection on a 118-program template).
"""

from __future__ import annotations

import random
import re

import pytest

from tai42_skeleton.states.templates import input_order, sibling_prelude


@pytest.mark.parametrize(
    ("bodies", "expected"),
    [
        ({"a": ".", "b": "."}, ["a", "b"]),
        ({"a": "tjq_b({})", "b": "tjq_c({})", "c": "."}, ["c", "b", "a"]),
        (
            {"top": "tjq_l({}) + tjq_r({})", "l": "tjq_base({})", "r": "tjq_base({})", "base": "1"},
            ["base", "l", "r", "top"],
        ),
        ({"x": "tjq_y({})", "y": "tjq_x({})", "z": "."}, ["z", "x", "y"]),
        ({"a__b": "tjq_c__d({})", "c__d": ".", "a": "tjq_a__b({})"}, ["c__d", "a__b", "a"]),
        ({"a": "tjq_ab({}) | xtjq_b", "ab": ".", "b": "tjq_a({})"}, ["ab", "a", "b"]),
        ({"self": "tjq_self({})"}, ["self"]),
        ({}, []),
    ],
)
def test_hand_written_orders(bodies: dict[str, str], expected: list[str]) -> None:
    assert input_order(bodies) == expected


def _selection_order(bodies: dict[str, str]) -> list[str]:
    """The step-by-step selection: at each step the smallest name whose callees are all emitted."""

    def refers(expr: str, name: str) -> bool:
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", expr) is not None

    names = sorted(bodies)
    deps = {a: {b for b in names if b != a and refers(bodies[a], f"tjq_{b}")} for a in names}
    ordered: list[str] = []
    remaining = set(names)
    while remaining:
        ready = sorted(n for n in remaining if deps[n] <= set(ordered))
        if not ready:
            ordered.extend(sorted(remaining))
            break
        ordered.append(ready[0])
        remaining.discard(ready[0])
    return ordered


def test_a_118_program_template_orders_as_the_step_selection() -> None:
    rng = random.Random(7)
    names = [f"p{i:03d}_{rng.choice('abcxyz')}" for i in range(118)]
    rng.shuffle(names)
    bodies = {}
    for i, name in enumerate(names):
        refs = rng.sample(names[:i], k=min(i, rng.randint(0, 3)))
        bodies[name] = " + ".join(f"tjq_{r}({{}})" for r in refs) or "0"
    order = input_order(bodies)
    assert order == _selection_order(bodies)
    position = {name: i for i, name in enumerate(order)}
    for name, body in bodies.items():
        for callee in re.findall(r"tjq_([A-Za-z0-9_]+)", body):
            assert position[callee] < position[name]


def test_the_sibling_prelude_follows_the_order() -> None:
    bodies = {"a": "tjq_b({})", "b": "1"}
    assert sibling_prelude(bodies, input_order(bodies)) == "def tjq_b($params): 1; def tjq_a($params): tjq_b({}); "
