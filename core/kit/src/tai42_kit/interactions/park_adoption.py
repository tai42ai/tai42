"""The chain-fire notice a driver sends when a run that ran nested under another driver reaches its terminal.

A run started inside another driver's chained call re-enters that waiting run at its own
terminal by firing the chain-delivery tool the platform bound around the call
(:class:`~tai42_contract.interactions.ChainedResume`). Every driver builds the fire here, so the
payload has one shape: the chain key, the result, and the status word of the contract.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.interactions import (
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    ChainedResume,
    RunFailed,
)

__all__ = ["terminal_chain_notice"]


def terminal_chain_notice(routing: ChainedResume, result: Any) -> tuple[str, dict[str, Any]]:
    """The ``(delivery tool, payload)`` a run's terminal fires along its captured chain routing.

    A :class:`~tai42_contract.interactions.RunFailed` result is a failed terminal: its opaque
    ``outcome`` rides as the ``result`` under the ``failed`` status. Any other result is a
    success, carried whole. The payload keys are exactly the chain key, ``result`` and
    ``status``.
    """
    if isinstance(result, RunFailed):
        payload = {
            CHAINED_PARK_TOKEN_KEY: routing.chain_key,
            "result": result.outcome,
            "status": PARK_COMPLETION_FAILED,
        }
    else:
        payload = {CHAINED_PARK_TOKEN_KEY: routing.chain_key, "result": result, "status": PARK_COMPLETION_SUCCEEDED}
    return routing.delivery_tool, payload
