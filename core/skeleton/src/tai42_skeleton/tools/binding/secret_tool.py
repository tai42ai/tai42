"""The FastMCP tool subclass that reveals wrapped secrets (and re-emits the suspended-interaction marker)."""

from typing import Any

from fastmcp.tools.base import ToolResult
from fastmcp.tools.function_tool import FunctionTool
from tai42_contract.interactions import SuspendedInteraction, suspended_interaction_marker
from tai42_contract.secrets import contains_secrets, mask_secrets, unwrap_secrets

from tai42_skeleton.tools.reveal_gate import inprocess_reveal_gate, note_secret_reveal


class _SecretRevealingTool(FunctionTool):
    """A FastMCP tool that reveals wrapped secrets in its return before the MCP ``tools/call`` result is serialized.

    ``convert_result`` has three present-tense modes, keyed on the in-process
    reveal gate:

    * gate unarmed (a genuine MCP ``tools/call`` — main server, sub-MCP, a direct
      tool OR a preset reached FastMCP→``TransformedTool.run``) — REVEAL: the live
      caller gets the real value.
    * gate armed, the return carries a secret (an in-process preset dispatch whose
      forwarding fn re-entered this parent ``run``) — stow the RAW wrapper-bearing
      value on the gate and return a ToolResult of only the placeholder, so the
      dispatch hands its caller the wrapper intact while the ToolResult that flows
      back through the transform carries no leak and cannot crash on serialize.
    * gate armed, no secret — pass the value through unchanged, so a non-secret
      preset keeps the exact ``_tool_result_value`` path with zero drift.

    The reveal must land here, before FastMCP's own serialization (which has no
    secret-aware step: ``ToolResult`` would drop a ``SecretValue`` — structured
    serialize raises, the text block masks it), not in a result-transforming
    middleware that only sees the already-serialized ``ToolResult``.
    """

    def convert_result(self, raw_value: Any) -> ToolResult:
        gate = inprocess_reveal_gate.get()
        if gate is not None:
            if isinstance(raw_value, SuspendedInteraction):
                # An async ask_user through a preset parks the caller and returns this
                # sentinel. FastMCP's serialization would FLATTEN the pydantic model into
                # ``structured_content`` (a plain dict), so the dispatch's type-based park
                # recognition fails and the flow proceeds UN-parked while the delivered
                # question's later answer strands — a silent lost-park. Stow it RAW so the
                # in-process dispatch returns the object intact, exactly as the direct-run
                # seam preserves it; the ToolResult built below is never returned.
                gate.park = raw_value
                gate.has_park = True
                return super().convert_result(raw_value)
            if contains_secrets(raw_value):
                gate.payload = raw_value
                gate.has_payload = True
                return super().convert_result(mask_secrets(raw_value))
            return super().convert_result(raw_value)
        if isinstance(raw_value, SuspendedInteraction):
            # Unarmed MCP edge: an async ask_user through this tool (or a preset over it)
            # parked the caller and returned this sentinel. FastMCP's own serialization
            # would FLATTEN the pydantic model into ``structured_content`` (its plain
            # fields), dropping the reserved marker key a dispatch edge recognizes a park
            # by — so the park would record ``success``. Emit the same reserved marker the
            # in-graph seam commits, as the result's ``structured_content``, so
            # ``DispatchScopeMiddleware`` records the park by its interaction id at this
            # edge exactly as the in-process seam records it by the sentinel's TYPE. Built
            # as the ToolResult's structured content directly (never routed through the
            # tool's output_schema) since the marker is a control signal, not the tool's
            # declared output — it must ride the wire top-level, unwrapped, whatever the
            # base tool's return schema.
            marker = suspended_interaction_marker(raw_value.interaction_id, raw_value.expiry_at, raw_value.resume_owner)
            return ToolResult(structured_content=marker)
        if contains_secrets(raw_value):
            # Unarmed MCP edge: this reveal exposes a secret into ``structured_content``.
            # Flag it so the preset output-schema guard redacts any validation failure
            # rather than let the plaintext ride the raised error into fastmcp's logs.
            note_secret_reveal()
        return super().convert_result(unwrap_secrets(raw_value))
