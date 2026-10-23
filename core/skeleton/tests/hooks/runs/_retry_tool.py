"""A tool declaring a retry policy, for the hook-run tests that start it through the real tool dispatch.

``calls`` records each body invocation; a test clears it before firing.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.tools import ToolRetryBackoff, ToolRetryPolicy

calls: list[int] = []


@tai42_app.tools.tool(
    retry=ToolRetryPolicy(
        max_attempts=3,
        idempotent=True,
        backoff=ToolRetryBackoff(initial_seconds=0.001, multiplier=1.0, cap_seconds=0.001),
    )
)
def retried_probe(fail_times: int) -> dict:
    """Raise ``ConnectionError`` on the first ``fail_times`` invocations, then answer."""
    calls.append(fail_times)
    if len(calls) <= fail_times:
        raise ConnectionError("upstream down")
    return {"attempts": len(calls)}
