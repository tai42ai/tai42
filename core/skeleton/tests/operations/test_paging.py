"""The one page/page-size arithmetic the paged read doors share."""

from __future__ import annotations

import pytest

from tai42_skeleton.operations._paging import next_page, page_bounds
from tai42_skeleton.operations.errors import BadRequestError


def test_page_bounds_names_offset_and_limit_and_caps_the_size() -> None:
    assert page_bounds(1, 10, max_page=5, max_page_size=50) == (0, 10)
    assert page_bounds(3, 10, max_page=5, max_page_size=50) == (20, 10)
    assert page_bounds(2, 500, max_page=5, max_page_size=50) == (50, 50)


@pytest.mark.parametrize(
    ("page", "page_size", "message"),
    [
        (0, 10, "page and page_size must be >= 1, got page=0 page_size=10"),
        (1, 0, "page and page_size must be >= 1, got page=1 page_size=0"),
        (6, 10, "page must be <= 5, got page=6"),
    ],
)
def test_page_bounds_refuses_out_of_range(page: int, page_size: int, message: str) -> None:
    with pytest.raises(BadRequestError) as excinfo:
        page_bounds(page, page_size, max_page=5, max_page_size=50)
    assert excinfo.value.message == message


def test_next_page_reads_the_total() -> None:
    assert next_page(1, 10, 25) == 2
    assert next_page(3, 10, 25) is None
    assert next_page(2, 10, 20) is None
