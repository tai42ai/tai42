"""Chain execution for the ``chain`` tool extension.

Calls a tool, transforms its output with a jq expression, then calls a second
named tool with the transformed result. The composed signature the extension
presents lives with its factory; this module holds only the runner.

Needs the ``chain`` extra (jq, via ``tai42-kit[jq]``); the module fails loudly at
import with an install hint when jq is absent, never a silent skip.
"""

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction
from tai42_kit.utils.data import run_jq_first

try:
    import jq  # noqa: F401
except ImportError as exc:
    raise ImportError(
        "The 'chain' tool extension needs the jq library. Install it with: pip install 'tai42-toolbox[chain]'"
    ) from exc


async def execute_chain(
    tool_name: str,
    tool_arguments: dict[str, Any],
    jq_expression: str,
    next_tool_name: str,
) -> Any:
    """Call ``tool_name``, transform its output with ``jq_expression``, then call
    ``next_tool_name`` with the transformed result.

    If the first stage async-parks it returns the ``SuspendedInteraction`` park SIGNAL; the
    chain PROPAGATES it verbatim instead of feeding the sentinel to jq (which would fail to
    read it as data) or handing it to the next stage. The chain parks as a whole and its own
    resumer drives the parked stage forward; the caller's park recognition fires on the
    surfaced signal. The second stage's result is returned as-is, so a park there propagates
    naturally by being the return value."""
    first_result = await tai42_app.tools.run_tool(tool_name, tool_arguments)
    # A parked first stage surfaces the sentinel; propagate it rather than feeding a park
    # signal into jq or the next tool. The chain re-surfaces exactly one park.
    if isinstance(first_result, SuspendedInteraction):
        return first_result
    jq_result = await run_jq_first(jq_expression, first_result)
    return await tai42_app.tools.run_tool(next_tool_name, jq_result)
