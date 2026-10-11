"""The one page/page-size arithmetic every paged read door shares; each caller passes its own caps."""

from __future__ import annotations

from tai42_skeleton.operations.errors import BadRequestError


def page_bounds(page: int, page_size: int, *, max_page: int, max_page_size: int) -> tuple[int, int]:
    """The ``(offset, limit)`` a page/page-size pair names.

    Both must be at least 1 and ``page`` at most ``max_page`` (a ``BadRequestError`` otherwise); a
    page size above ``max_page_size`` is capped, never refused.
    """
    if page < 1 or page_size < 1:
        raise BadRequestError(f"page and page_size must be >= 1, got page={page} page_size={page_size}")
    if page > max_page:
        raise BadRequestError(f"page must be <= {max_page}, got page={page}")
    limit = min(page_size, max_page_size)
    return (page - 1) * limit, limit


def next_page(page: int, limit: int, total: int) -> int | None:
    """The next page number, or ``None`` on the last page.

    Read from the caller's total, not the returned count: a page shortened by rows that vanished
    under it is not the end.
    """
    return page + 1 if page * limit < total else None
