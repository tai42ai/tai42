"""C1 / M1 — the writer/reader split across processes: a tool call increments a
counter in a real worker process; the separate metrics-server process scrapes it
from the shared mmap dir. This single test is the one that catches M1 at its
root."""

from __future__ import annotations

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import PROBE_TOOLS_TITLE
from tai42_e2e.stack import TaiStack

_FAMILY = "tai_tool_call_count_total"
_LABELS = {"name": "e2e_echo", "runtime": "main", "title": PROBE_TOOLS_TITLE}


async def test_tool_call_metrics_visible_in_separate_scraper(core_stack: TaiStack) -> None:
    baseline = core_stack.scrape().sample(_FAMILY, _LABELS) or 0.0

    async with core_stack.mcp() as mcp:
        for _ in range(5):
            await mcp.call_tool("e2e_echo_prometheus_metrics", {"payload": "hi"})

    async def scraped_delta() -> bool:
        current = core_stack.scrape().sample(_FAMILY, _LABELS)
        return current is not None and current - baseline >= 5

    await wait_for_async(
        scraped_delta,
        deadline=5.0,
        message="metrics server never reflected the 5 e2e_echo calls",
    )
    # Exactly 5 — from the separate scraper process and from the in-app /metrics
    # route reading the same shared dir (an over-count would flag a double
    # registration the ">= 5" form would let through).
    assert core_stack.scrape().require(_FAMILY, _LABELS) - baseline == 5
    assert core_stack.app_scrape().require(_FAMILY, _LABELS) - baseline == 5


async def test_error_and_duration_samples(core_stack: TaiStack) -> None:
    error_labels = {"name": "e2e_fail", "runtime": "main", "title": PROBE_TOOLS_TITLE}
    duration_count = "tai_tool_run_time_seconds_count"
    base_errors = core_stack.scrape().sample("tai_tool_error_count_total", error_labels) or 0.0
    base_count = core_stack.scrape().sample(duration_count, error_labels) or 0.0

    async with core_stack.mcp() as mcp:
        result = await mcp.call_tool("e2e_fail_prometheus_metrics", {"message": "boom"}, raise_on_error=False)
    assert result.is_error, "e2e_fail must surface an MCP error result, not a transport failure"

    async def scraped() -> bool:
        scrape = core_stack.scrape()
        errors = scrape.sample("tai_tool_error_count_total", error_labels)
        count = scrape.sample(duration_count, error_labels)
        return errors is not None and count is not None and errors - base_errors >= 1 and count - base_count >= 1

    await wait_for_async(scraped, deadline=5.0, message="error/duration samples never incremented for e2e_fail")
