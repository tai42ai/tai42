"""Observability READ bounds against the compose-provided self-hosted Langfuse:
a too-broad read, a genuinely unsupported read, and a valid-but-empty read are
three distinct answers on ``/api/observability/runs``.

A read whose window/paging is too broad for the backend to resolve answers 400
``monitoring-read-too-broad`` (the caller narrows the request); a read the
backend cannot serve at all answers 501 ``monitoring-read-not-supported`` (the
Studio install path); a valid, bounded filter that simply matches nothing
answers a normal 200 empty page. Opt-in: collects only with
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

# The wire error codes the observability doors stamp so the UI keys its dedicated
# states on them: a too-broad read (400) carries the first; a read the backend
# cannot serve (501) the second.
_READ_TOO_BROAD_CODE = "monitoring-read-too-broad"
_READ_NOT_SUPPORTED_CODE = "monitoring-read-not-supported"


def _window_start() -> str:
    """A whole-second ISO instant used as the run list's ``from`` bound, so every
    read in this module is time-bounded (never an unbounded scan)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


async def test_too_broad_metric_page_is_400_not_the_install_path(monitoring_stack: TaiStack) -> None:
    # A metric-sorted run list whose page reaches past the backend's metric-rank row
    # cap is too broad to resolve: the reader raises MonitoringReadTooBroadError and
    # the shared skeleton translator maps it to 400 monitoring-read-too-broad — the
    # caller narrows the request, never the 501 install page.
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


async def test_unsupported_metric_sort_filter_is_501_install_path(monitoring_stack: TaiStack) -> None:
    # A metric sort combined with a status=error filter is a capability the metrics
    # traces view does not have: the ``status`` query param maps to a trace ``level``
    # clause, and the metrics traces view carries no level column. The backend declares
    # the incompatibility, the reader raises MonitoringReadNotSupportedError, and the
    # skeleton maps it to 501 monitoring-read-not-supported (the install path).
    api: ApiClient = monitoring_stack.api()
    caps = await api.get("/api/observability/capabilities", retry_on_reloading=True)
    # The backend's own declaration: cost sort is incompatible with the ``level`` clause
    # (what ``status=error`` sets). The capability names the filter FIELD, ``level``.
    assert "level" in caps["incompatibleFilters"].get("cost", []), caps["incompatibleFilters"]

    # page 1 stays within the rank row cap, so the read is refused for the unsupported
    # filter combination, not for being too broad.
    query = urlencode({"from": _window_start(), "sort": "cost", "dir": "desc", "status": "error", "page": 1})
    body = await api.request("GET", f"/api/observability/runs?{query}", expect=501, retry_on_reloading=True)

    assert body["code"] == _READ_NOT_SUPPORTED_CODE, body
    assert body.get("error"), body


async def test_valid_empty_filter_is_200_empty_page(monitoring_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    # A valid, bounded native listing filtered to a run user no trace carries matches
    # nothing: the reader returns an empty page and the door answers a normal 200 empty
    # list — not the too-broad 400 and not the not-supported 501.
    api: ApiClient = monitoring_stack.api()
    absent_user = uniq("nouser")

    query = urlencode({"from": _window_start(), "user": absent_user, "pageSize": 50})
    page = await api.get(f"/api/observability/runs?{query}", retry_on_reloading=True)

    assert page["items"] == [], page
    assert page["page"] == 1
    assert page["nextPage"] is None
