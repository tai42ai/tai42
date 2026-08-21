"""The preset bind kernel — the single point every preset builds its live tool
through.

``preset_bind`` transforms a base tool into a new named tool via ONE FastMCP
``Tool.from_tool`` call: each ``fixed_kwargs`` key is baked as a HIDDEN, FIXED
constant (``ArgTransform(hide=True, default=<value>)`` — removed from the exposed
input schema, and a caller that passes it is rejected; it cannot be overridden at
runtime), while the REMAINING arguments keep the base tool's real typed schema
(names, types, descriptions), NOT one opaque ``params`` blob. The preset's
``description`` is set on the transformed tool, so a bind re-applies it from the
stored body every time. A preset carries NO native tags — grouping is the
tool_meta overlay's job.

Bake through the PROGRAMMATIC ``transform_args`` path (whose ``default`` accepts
any value, incl. dict/list) rather than a declarative scalar-only path, so a
non-scalar baked value is preserved.

An author-set ``output_schema`` (an object JSON Schema) dispatches on the base's
kind. When the base is an AGENT run tool, the schema is baked into the run tool's
``response_format`` (a hidden, fixed constant, with the preset ``name`` injected as
its ``title`` when absent — the agent run seam requires a title, and the preset
name is validated to the provider structured-output name charset) so the agent
FORCES a structured output; the preset advertises the authored (title-free)
schema. When the base is a plain tool, the schema is advertised as the bound
tool's output schema and every result is validated against it at run time with
tai42-kit's faithful draft-2020-12 validator, raising loudly on any mismatch — no
forcing is possible for a non-LLM tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform, forward
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.secrets import SECRET_PLACEHOLDER, unwrap_secrets
from tai42_kit.utils.data.json_schema_util import (
    JsonSchemaValidationError,
    validate_against_json_schema,
)

from tai42_skeleton.tools.reveal_gate import secret_was_revealed, stowed_park, stowed_reveal_payload

if TYPE_CHECKING:
    from tai42_contract.app import TaiApp


async def preset_bind(
    app: TaiApp,
    base_tool: str,
    fixed_kwargs: dict[str, Any],
    *,
    name: str,
    description: str = "",
    output_schema: dict[str, Any] | None = None,
) -> Tool:
    """Return a FastMCP tool transform of ``base_tool`` as the new named tool ``name``.

    Resolves the base ``Tool`` object (``app.tools.get_tool`` — hence async), then
    builds the transform in one call. Each ``fixed_kwargs`` key becomes a hidden,
    fixed constant; the remaining arguments keep the base tool's typed schema. An
    ``output_schema`` dispatches on the base kind (agent → bake ``response_format``;
    plain tool → advertise + validate-and-raise).
    """
    base = await app.tools.get_tool(base_tool)
    transform_args = {key: ArgTransform(hide=True, default=value) for key, value in fixed_kwargs.items()}

    if output_schema is None:
        return Tool.from_tool(
            base,
            name=name,
            description=description,
            transform_args=transform_args,
        )

    if base_tool in app.agents.all_agents():
        # Agent base — FORCE structured output: bake ``response_format`` from the
        # authored schema. The agent run seam requires a top-level ``title``; when
        # the author left it off, inject the preset name. The advertised output
        # schema stays the authored (title-free) value; the agent's own drain
        # validates the forced result, so no second validation wrapper is attached.
        baked_response_format = dict(output_schema)
        baked_response_format.setdefault("title", name)
        transform_args["response_format"] = ArgTransform(hide=True, default=baked_response_format)
        return Tool.from_tool(
            base,
            name=name,
            description=description,
            transform_args=transform_args,
            output_schema=output_schema,
        )

    # Plain tool — DECLARE + VALIDATE: advertise the authored schema and validate
    # every result against it, raising loudly on any mismatch (a non-LLM tool
    # cannot be forced).
    def _raise_redacted(caught: JsonSchemaValidationError) -> None:
        # The placeholder-only failure: json_path kept, instance text replaced by the
        # placeholder. MUST be invoked OUTSIDE the ``except`` and raise ``from None`` so
        # neither ``__cause__`` nor ``__context__`` retains the caught error, whose
        # message and ``offending_value`` embed the revealed secret verbatim and would
        # otherwise ride a logged traceback.
        raise JsonSchemaValidationError(
            f"value does not match schema at {caught.json_path}: a secret-bearing "
            f"result violates the schema ({SECRET_PLACEHOLDER} redacts the instance text)",
            json_path=caught.json_path,
            offending_value=SECRET_PLACEHOLDER,
        ) from None

    async def _enforce_output_schema(**kwargs: Any) -> Any:
        result = await forward(**kwargs)
        # A park sentinel is a SUSPEND signal, not the tool's output: an async
        # ask_user parked the caller and the dispatch's reveal gate stowed the
        # ``SuspendedInteraction`` RAW (the value that flowed back through the
        # transform is its flattened ToolResult). Output-schema validation must NOT
        # apply to the park — recognized on the gate by TYPE, it passes through
        # untouched so the dispatch returns the sentinel object intact, exactly as
        # the other dispatch paths preserve it; the real ANSWER is validated on resume.
        has_park, park = stowed_park()
        if has_park and isinstance(park, SuspendedInteraction):
            return result
        # Under the armed in-process reveal gate a secret-bearing return is stowed
        # RAW while ``result.structured_content`` carries only the masked projection;
        # validate the REVEALED value (an in-memory reveal for validation only —
        # nothing recorded, nothing returned from here) so the guard checks the real
        # result, not the placeholder. With no stowed payload the structured content
        # is already the real value.
        has_payload, payload = stowed_reveal_payload()
        if has_payload:
            revealed = unwrap_secrets(payload)
            caught: JsonSchemaValidationError | None = None
            try:
                validate_against_json_schema(revealed, output_schema)
            except JsonSchemaValidationError as exc:
                caught = exc
            if caught is not None:
                _raise_redacted(caught)
        else:
            # Unarmed MCP edge: ``structured_content`` already carries the REVEALED
            # value. A validation failure whose secret was revealed on this door
            # (:func:`secret_was_revealed`) is redacted the same both-links-severed
            # way; a non-secret preset re-raises untouched, keeping its instance-quoting.
            caught = None
            try:
                validate_against_json_schema(result.structured_content, output_schema)
            except JsonSchemaValidationError as exc:
                if not secret_was_revealed():
                    raise
                caught = exc
            if caught is not None:
                _raise_redacted(caught)
        return result

    return Tool.from_tool(
        base,
        name=name,
        description=description,
        transform_args=transform_args,
        output_schema=output_schema,
        transform_fn=_enforce_output_schema,
    )
