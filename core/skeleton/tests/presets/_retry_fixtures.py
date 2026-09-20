"""Base tool for the runs-index-under-retry integration test.

A ``flaky_fetch`` tool declaring a retry policy at registration — the policy a
preset baked over it inherits through the dispatch seam's parent walk. Kept in
its own module so no single module is imported by two manifest sections (the
preset-engine convention). ``calls`` records each body invocation; a test clears
it before dispatching.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.tools import ToolRetryBackoff, ToolRetryPolicy

calls: list[str] = []


@tai42_app.tools.tool(
    retry=ToolRetryPolicy(
        max_attempts=3,
        idempotent=True,
        backoff=ToolRetryBackoff(initial_seconds=0.001, multiplier=1.0, cap_seconds=0.001),
    )
)
def flaky_fetch(city: str, fail_times: int = 2) -> dict:
    """Fail the first ``fail_times`` invocations with a transient delivery error,
    then answer — the retry-to-success shape."""
    calls.append(city)
    if len(calls) <= fail_times:
        raise ChannelDeliveryError("medium 503", retryable=True)
    return {"city": city, "attempts": len(calls)}
