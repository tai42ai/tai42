"""Shape/overlap/collision algebra for one-owner-per-route enforcement.

Exhaustive overlap matrix — literal/template/rest-converter positions, differing
segment counts, and the ``{x:path}`` conservative rest-overlap — plus the
method-intersection collision rule.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.route_shapes import (
    Literal,
    Param,
    PathParam,
    collision,
    overlap,
    parse_shape,
)


def test_parse_literal_param_and_pathparam_segments() -> None:
    assert parse_shape("/api/channels/web/chat/{id}") == (
        Literal("api"),
        Literal("channels"),
        Literal("web"),
        Literal("chat"),
        Param(),
    )
    # A non-``:path`` converter is a single-segment Param; ``:path`` is a PathParam.
    assert parse_shape("/api/x/{n:int}/{rest:path}") == (Literal("api"), Literal("x"), Param(), PathParam())
    # The root path is the empty shape; a leading slash yields no empty segment.
    assert parse_shape("/") == ()
    assert parse_shape("/health") == (Literal("health"),)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # Equal literals overlap; differing literals do not.
        ("/api/x", "/api/x", True),
        ("/api/x", "/api/y", False),
        # A concrete path instantiating a template overlaps it (a collision).
        ("/api/x/5", "/api/x/{id}", True),
        # Two templates at the same position overlap.
        ("/api/x/{a}", "/api/x/{b}", True),
        # A literal vs a template at one position, equal literals elsewhere.
        ("/api/x/foo", "/api/{a}/foo", True),
        # Differing segment counts never overlap (without a rest-converter).
        ("/api/x", "/api/x/y", False),
        ("/api/x/y/z", "/api/x/y", False),
        # A differing literal anywhere blocks the overlap despite a shared template.
        ("/api/x/{a}", "/api/y/{b}", False),
    ],
)
def test_overlap_matrix(a: str, b: str, expected: bool) -> None:
    assert overlap(parse_shape(a), parse_shape(b)) is expected
    # Overlap is symmetric.
    assert overlap(parse_shape(b), parse_shape(a)) is expected


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        # A ``:path`` at index 0 overlaps every candidate (>= 0 segments).
        ("/anything", True),
        ("/deep/nested/path", True),
        ("/", True),
    ],
)
def test_root_rest_converter_overlaps_any_suffix(candidate: str, expected: bool) -> None:
    catch_all = parse_shape("/{spa_path:path}")
    assert overlap(parse_shape(candidate), catch_all) is expected


def test_interior_rest_converter_overlaps_matching_prefix_only() -> None:
    # ``/api/plugins/{name}/studio/{path:path}``: the fixed prefix must agree; the
    # rest-converter then swallows any suffix of >= its index segments.
    core = parse_shape("/api/plugins/{name}/studio/{path:path}")
    assert overlap(parse_shape("/api/plugins/acme/studio/assets/app.js"), core) is True
    assert overlap(parse_shape("/api/plugins/acme/studio"), core) is True
    # A different fixed prefix does not overlap however long the candidate.
    assert overlap(parse_shape("/api/tools/acme/studio/x"), core) is False


def test_rest_converter_requires_enough_segments_to_span_the_fixed_frame() -> None:
    # A concrete path SHORTER than the rest-converter's index cannot reach the
    # fixed segments the rest frames, so it does not overlap.
    studio = parse_shape("/api/plugins/{name}/studio/{path:path}")
    assert overlap(parse_shape("/api/plugins/foo"), studio) is False
    # One more segment still falls short of the ``/studio/`` segment at index 4.
    assert overlap(parse_shape("/api/plugins/foo/studio"), studio) is True
    assert overlap(parse_shape("/api/plugins/foo/tools"), studio) is False


def test_non_terminal_rest_converter_honours_trailing_literal() -> None:
    # Real core routes carry a literal AFTER the rest: ``.../{id:path}/stat`` vs
    # ``.../{id:path}/content``. The trailing literal must align positionally.
    stat = parse_shape("/api/storage/resources/{resource_id:path}/stat")
    content = parse_shape("/api/storage/resources/{resource_id:path}/content")
    assert overlap(parse_shape("/api/storage/resources/a/b/content"), stat) is False
    assert overlap(parse_shape("/api/storage/resources/a/b/stat"), stat) is True
    assert overlap(parse_shape("/api/storage/resources/a/stat"), stat) is True
    # An empty rest span is allowed, so prefix + trailing literal alone overlaps.
    assert overlap(parse_shape("/api/storage/resources/stat"), stat) is True
    # A candidate too short for prefix + trailing literal does not overlap.
    assert overlap(parse_shape("/api/storage/resources"), stat) is False
    # Two non-terminal rests differing only in the trailing literal never overlap.
    assert overlap(stat, content) is False
    # Same trailing literal and prefix: the swallowed middles overlap.
    assert overlap(stat, parse_shape("/api/storage/resources/{other:path}/stat")) is True


def test_collision_requires_shape_overlap_and_method_intersection() -> None:
    a = parse_shape("/api/x/{id}")
    b = parse_shape("/api/x/5")
    # Overlapping shapes + intersecting methods collide.
    assert collision(a, frozenset({"GET"}), b, frozenset({"GET"})) is True
    # Overlapping shapes but DISJOINT methods do not collide.
    assert collision(a, frozenset({"GET"}), b, frozenset({"POST"})) is False
    # Non-overlapping shapes never collide even with shared methods.
    c = parse_shape("/api/y/5")
    assert collision(a, frozenset({"GET"}), c, frozenset({"GET"})) is False
