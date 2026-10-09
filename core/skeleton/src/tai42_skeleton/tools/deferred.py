"""The ``tool`` deferred-call kind: a tool call staged in a states unit and run after the reply.

Captured with the caller's execution identity, run attribution, state context and trace; run as
an outermost ``run_tool`` dispatch under those, so authorization, the declared retry policy, the
runs-index recording and the trace all apply. The call reads its stable idempotency key from
``tai42_app.tools.extras()["tai42/idempotency_key"]``.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.monitoring import RunAttribution, TraceContext, ambient_trace_context
from tai42_contract.states.errors import DeferredCallRefusedError
from tai42_contract.states.models import StateContext

from tai42_skeleton.authz.execution_identity import capture_fire_identity
from tai42_skeleton.states.context import current_state_context, state_context
from tai42_skeleton.states.outbox.calls import register_deferred_call_kind
from tai42_skeleton.tools.attribution import get_run_attribution, run_attribution
from tai42_skeleton.tools.binding.errors import UnknownToolError

# The door ``extras`` key carrying a deferred call's stable idempotency key.
IDEMPOTENCY_KEY_EXTRA = "tai42/idempotency_key"


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
        from tai42_skeleton.monitoring import get_monitoring

        return {
            "tool": target,
            "arguments": arguments,
            "identity": [user_id, fingerprint] if user_id is not None else None,
            "attribution": attribution.model_dump(mode="json") if attribution is not None else None,
            "state_context": ctx.model_dump(mode="json") if ctx is not None else None,
            "trace_id": get_monitoring().writer.current_trace_id(),
        }

    async def apply(self, payload: dict[str, Any], *, idempotency_key: str) -> None:
        """Run the captured call under its captured identity, attribution, state context and trace.

        A rotated, disabled or de-scoped key refuses loudly; a call that parks is refused, since a
        call after the reply cannot wait for an answer.
        """
        tool = payload["tool"]
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
                stack.enter_context(ambient_trace_context(TraceContext(trace_id=payload["trace_id"])))
            result = await tai42_app.tools.run_tool(
                tool, payload["arguments"], extras={IDEMPOTENCY_KEY_EXTRA: idempotency_key}
            )
        outcome = await tai42_app.interactions.normalise_started(result)
        if outcome.kind in ("asks", "parked"):
            raise DeferredCallRefusedError(
                f"deferred call {tool!r} parked; a call after the reply cannot wait for an answer"
            )

    async def resumable(self, payload: dict[str, Any]) -> bool:
        """Whether the tool's registration declares crash-resume."""
        # The operations package registers app handlers at import; it is reached at call time.
        from tai42_skeleton.operations.tool_runs.models import _CRASH_RESUME_META_KEY

        try:
            tool = await tai42_app.tools.get_tool(payload["tool"])
        except UnknownToolError:
            return False
        return bool((tool.meta or {}).get(_CRASH_RESUME_META_KEY))


register_deferred_call_kind("tool", ToolCallKind())
