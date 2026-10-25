"""The ``tool`` deferred-call kind: a tool call staged in a states unit and run after the reply.

Captured with the caller's execution identity, run attribution, state context and trace parent; run
as an outermost ``run_tool`` dispatch under those, so authorization, the declared retry policy and the
runs-index recording apply, and the call records in the caller's trace under a ``deferred-call:<tool>``
span that is a child of the span live when it was deferred. The call reads its stable idempotency key
from ``tai42_app.tools.extras()["tai42/idempotency_key"]``.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.monitoring import RunAttribution, TraceContext
from tai42_contract.states.errors import DeferredCallRefusedError
from tai42_contract.states.models import StateContext
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY, TOOL_META_CRASH_RESUME
from tai42_kit.monitoring import RunTraceRoot, open_lineage_span

from tai42_skeleton.authz.execution_identity import capture_fire_identity
from tai42_skeleton.states.context import current_state_context, state_context
from tai42_skeleton.states.outbox.calls import register_deferred_call_kind
from tai42_skeleton.tools.attribution import get_run_attribution, run_attribution, stamp_run_attribution
from tai42_skeleton.tools.binding.errors import UnknownToolError


class ToolCallKind:
    """Capture and run a deferred tool call."""

    async def capture(self, target: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The payload that runs ``target`` with ``arguments`` later, in the caller's identity and context."""
        try:
            await tai42_app.tools.get_tool(target)
        except UnknownToolError as exc:
            raise DeferredCallRefusedError(f"deferred call names unknown tool {target!r}") from exc
        try:
            json.dumps(arguments)
        except (TypeError, ValueError) as exc:
            raise DeferredCallRefusedError(
                f"deferred call to {target!r} carries arguments that are not plain JSON: {exc}"
            ) from exc
        user_id, fingerprint = capture_fire_identity()
        attribution = get_run_attribution()
        ctx = current_state_context()
        from tai42_skeleton.monitoring import current_trace_parent

        parent = current_trace_parent()
        return {
            "tool": target,
            "arguments": arguments,
            "identity": [user_id, fingerprint] if user_id is not None else None,
            "attribution": attribution.model_dump(mode="json") if attribution is not None else None,
            "state_context": ctx.model_dump(mode="json") if ctx is not None else None,
            "trace_id": parent.trace_id if parent is not None else None,
            "parent_span_id": parent.parent_span_id if parent is not None else None,
        }

    async def apply(self, payload: dict[str, Any], *, idempotency_key: str) -> None:
        """Run the captured call under its captured identity, attribution, state context and trace parent.

        With a captured parent the call runs inside a ``deferred-call:<tool>`` span, a child of that parent in
        the caller's trace, opened inside the captured attribution's trace scope so the span carries it even
        when the dispatch refuses before its own scope. The span records the call's arguments as its input and
        its result as its output; a raised error marks the span ``ERROR`` and propagates. Without a parent no
        span opens.

        A rotated, disabled or de-scoped key refuses loudly; a call that parks is refused inside its span,
        since a call after the reply cannot wait for an answer.
        """
        tool = payload["tool"]
        lineage: RunTraceRoot | None = None
        async with AsyncExitStack() as stack:
            if payload.get("attribution") is not None:
                stack.enter_context(run_attribution(RunAttribution.model_validate(payload["attribution"])))
            if payload.get("state_context") is not None:
                stack.enter_context(state_context(StateContext.model_validate(payload["state_context"])))
            if payload.get("identity") is not None:
                from tai42_skeleton.authz.execution import bind_execution_identity

                user_id, fingerprint = payload["identity"]
                await stack.enter_async_context(bind_execution_identity(user_id, bound_fingerprint=fingerprint))
            if payload.get("trace_id") is not None:
                stack.enter_context(stamp_run_attribution())
                parent = TraceContext(trace_id=payload["trace_id"], parent_span_id=payload["parent_span_id"])
                lineage = stack.enter_context(
                    open_lineage_span(f"deferred-call:{tool}", parent=parent, input_=payload["arguments"])
                )
            result = await tai42_app.tools.run_tool(
                tool, payload["arguments"], extras={TOOL_EXTRA_IDEMPOTENCY_KEY: idempotency_key}
            )
            outcome = await tai42_app.interactions.normalise_started(result)
            if outcome.kind in ("asks", "parked"):
                raise DeferredCallRefusedError(
                    f"deferred call {tool!r} parked; a call after the reply cannot wait for an answer"
                )
            if lineage is not None:
                lineage.record_output(result)

    async def resumable(self, payload: dict[str, Any]) -> bool:
        """Whether the tool's registration declares crash-resume."""
        try:
            tool = await tai42_app.tools.get_tool(payload["tool"])
        except UnknownToolError:
            return False
        return bool((tool.meta or {}).get(TOOL_META_CRASH_RESUME))


register_deferred_call_kind("tool", ToolCallKind())
