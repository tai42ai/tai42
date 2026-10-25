"""Observability READ bounds against the compose-provided self-hosted Langfuse on
``/api/observability/runs``.

A read whose window/paging is too broad for the backend to resolve answers 400
``monitoring-read-too-broad`` (the caller narrows the request); a metric sort
combined with any run-list filter answers a normal 200 page; a valid, bounded
filter that simply matches nothing answers a normal 200 empty page. Opt-in: collects only with
``TAI_E2E_MONITORING=1`` (the compose ``monitoring`` profile up); skipped at
collection otherwise."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# The monitoring stack runs no backend worker; skip this module on non-default
# backend legs (they exercise no backend seam).
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:monitoring:langfuse",
        "helper:langfuse-server",
        "setting:LANGFUSE_HOST=compose-langfuse",
    ),
]

# A metric sort (``cost`` / ``latencyMs`` / ``totalTokens``) is ranked globally
# through the Langfuse metrics API, whose per-query row budget caps at this many
# rows. A page whose reach (page * page size) exceeds the cap cannot be resolved
# and is answered as too-broad — mirrors the backend reader's documented metrics
# row cap, so a page over it trips the bound with no seeded data.
_METRIC_RANK_ROW_CAP = 1000

# The wire error code the observability doors stamp on a too-broad read (400), so
# the UI keys its dedicated state on it.
_READ_TOO_BROAD_CODE = "monitoring-read-too-broad"


def _window_start() -> str:
    """A whole-second ISO instant used as the run list's ``from`` bound, so every
    read in this module is time-bounded (never an unbounded scan)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


async def test_too_broad_metric_page_is_400_not_the_install_path(monitoring_stack: TaiStack) -> None:
    # A metric-sorted run list whose page reaches past the backend's metric-rank row
    # cap is too broad to resolve: the reader raises MonitoringReadTooBroadError and
    # the shared skeleton translator maps it to 400 monitoring-read-too-broad — the
    # caller narrows the request.
    api: ApiClient = monitoring_stack.api()
    caps = await api.get("/api/observability/capabilities", retry_on_reloading=True)
    page_size = caps["pageSizeMax"]
    # ``cost`` is one of the globally metric-ranked sorts this door exposes.
    assert "cost" in caps["sortKeys"], caps["sortKeys"]
    # reach = over_cap_page * page_size, chosen to clear the rank row cap.
    over_cap_page = (_METRIC_RANK_ROW_CAP // page_size) + 2

    query = urlencode(
        {"from": _window_start(), "sort": "cost", "dir": "desc", "page": over_cap_page, "pageSize": page_size}
    )
    body = await api.request("GET", f"/api/observability/runs?{query}", expect=400, retry_on_reloading=True)

    assert body["code"] == _READ_TOO_BROAD_CODE, body
    # The body carries the backend's own guidance, not an empty "no runs" answer.
    assert body.get("error"), body


# A valid run-list value for every filter query param a metric sort combines with:
# ``status=error`` sets the level clause; each cost/token/latency range is
# single-sided (never inverted), so each read is a well-formed, bounded filter.
_METRIC_SORT_FILTER_SAMPLES: dict[str, str] = {
    "status": "error",
    "version": "v1",
    "minCost": "0",
    "maxCost": "1",
    "minTokens": "0",
    "maxTokens": "1",
    "minLatencyMs": "0",
    "maxLatencyMs": "1000",
}


@pytest.mark.parametrize("param", sorted(_METRIC_SORT_FILTER_SAMPLES))
async def test_a_metric_sort_combines_with_every_filter(monitoring_stack: TaiStack, param: str) -> None:
    # A metric sort ranks runs globally and applies every run-list filter with it: the
    # backend declares no filter it cannot combine with the sort, and the read answers a
    # normal 200 page.
    api: ApiClient = monitoring_stack.api()
    caps = await api.get("/api/observability/capabilities", retry_on_reloading=True)
    assert "cost" in caps["sortKeys"], caps["sortKeys"]
    assert caps["incompatibleFilters"] == {}, caps["incompatibleFilters"]

    # page 1 stays within the rank row cap, so the read is never refused as too broad.
    query = urlencode(
        {"from": _window_start(), "sort": "cost", "dir": "desc", param: _METRIC_SORT_FILTER_SAMPLES[param], "page": 1}
    )
    page = await api.get(f"/api/observability/runs?{query}", retry_on_reloading=True)

    assert isinstance(page["items"], list), page
    assert page["page"] == 1, page


async def test_valid_empty_filter_is_200_empty_page(monitoring_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    # A valid, bounded native listing filtered to a run user no trace carries matches
    # nothing: the reader returns an empty page and the door answers a normal 200 empty
    # list, not the too-broad 400.
    api: ApiClient = monitoring_stack.api()
    absent_user = uniq("nouser")

    query = urlencode({"from": _window_start(), "user": absent_user, "pageSize": 50})
    page = await api.get(f"/api/observability/runs?{query}", retry_on_reloading=True)

    assert page["items"] == [], page
    assert page["page"] == 1
    assert page["nextPage"] is None
