"""The preset bind kernel — the single point every preset builds its live tool
through.

``preset_bind`` transforms a base tool into a new named tool via ONE FastMCP
``Tool.from_tool`` call: each ``fixed_kwargs`` key is baked as a HIDDEN, FIXED
constant (``ArgTransform(hide=True, default=<value>)`` — removed from the exposed
input schema, and a caller that passes it is rejected; it cannot be overridden at
runtime — with ONE exception: when a preset has ``input_schema`` support and bakes
the base tool's PAYLOAD ARG itself, that baked dict is not an absolute constant but a
set of DEFAULTS the caller's validated object deep-merges over, caller-wins-per-key
(:func:`deep_merge`); every OTHER baked kwarg stays an absolute hidden constant),
while the REMAINING arguments keep the base tool's real typed schema
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

import copy
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform, forward, forward_raw
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
    input_schema: dict[str, Any] | None = None,
) -> Tool:
    """Return a FastMCP tool transform of ``base_tool`` as the new named tool ``name``.

    Resolves the base ``Tool`` object (``app.tools.get_tool`` — hence async), then
    builds the transform in one call. Each ``fixed_kwargs`` key becomes a hidden,
    fixed constant; the remaining arguments keep the base tool's typed schema. An
    ``output_schema`` dispatches on the base kind (agent → bake ``response_format``;
    plain tool → advertise + validate-and-raise). An ``input_schema`` makes the exposed
    tool advertise the AUTHORED schema as its own input contract and routes the caller's
    validated object into the base tool's ``payload_arg`` (the base tool must have
    registered ``PresetInputSchemaSupport``; without it this is a loud error — the
    mechanism carries no base-tool knowledge).
    """
    base = await app.tools.get_tool(base_tool)
    transform_args = {key: ArgTransform(hide=True, default=value) for key, value in fixed_kwargs.items()}

    if input_schema is not None:
        return _bind_with_input_schema(
            app,
            base,
            base_tool,
            fixed_kwargs,
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
        )

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


def deep_merge(baked: dict[str, Any], caller: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``caller`` over ``baked`` with CALLER-WINS-PER-KEY semantics,
    recursing ONLY where both sides hold a dict.

    Applied per key:

    - key in both, both values dicts -> recurse (nested deep merge).
    - key in both, otherwise (scalars, lists, or mismatched types) -> the caller's
      value REPLACES the baked one (lists REPLACE, never concatenate; scalars are not
      coerced).
    - key only in ``baked`` -> the baked value fills the gap.
    - key only in ``caller`` -> the caller value passes through.

    Neither input is mutated; a fresh dict is returned. The result shares NO mutable state
    with ``baked``: a baked-only subtree (one no caller key overrides) is DEEP-COPIED into
    the result, not aliased. ``baked`` is a bind's shared payload defaults reused across every
    call, so aliasing it would let a consumer that mutates the forwarded merged payload poison
    those defaults for all later calls. Caller-supplied values, by contrast, come from a fresh
    per-call object and are taken by reference (a caller never shares state across calls).
    """
    # Baked-only keys are the sole aliasing seam (keys in ``caller`` are overwritten in the
    # loop below by either a recursion result or the caller's own value): deep-copy their
    # values so the returned object owns them and can never reach back into the shared defaults.
    merged: dict[str, Any] = {key: (value if key in caller else copy.deepcopy(value)) for key, value in baked.items()}
    for key, caller_value in caller.items():
        baked_value = baked.get(key)
        if key in baked and isinstance(baked_value, dict) and isinstance(caller_value, dict):
            merged[key] = deep_merge(baked_value, caller_value)
        else:
            merged[key] = caller_value
    return merged


def _bind_with_input_schema(
    app: TaiApp,
    base: Tool,
    base_tool: str,
    fixed_kwargs: dict[str, Any],
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
) -> Tool:
    """Build the exposed tool whose OWN advertised input schema is the authored
    ``input_schema``, validating the caller's object against it and routing the
    validated object into the base tool's ``payload_arg``.

    Mechanism only — no base-tool knowledge in the kernel. The base tool must have
    registered :class:`~tai42_contract.presets.PresetInputSchemaSupport`; a preset giving
    an ``input_schema`` to a base tool with no support is a LOUD error here (the shared
    authoring chokepoint rejects it first, but the kernel never silently ignores a schema).
    A custom ``transform_fn`` bypasses FastMCP's own argument validation (FastMCP validates
    a caller against ``.parameters`` ONLY for the identity transform, never for a custom
    fn), so ``_route`` MUST validate the caller's object against the authored schema itself
    before forwarding — a missing-required, wrong-typed, or ``additionalProperties``-forbidden
    field is rejected LOUDLY as a caller (client) error, never routed raw into the base tool.
    The validated object is forwarded to the base under ``payload_arg`` alongside the baked
    ``fixed_kwargs`` (``forward_raw`` — the base's own arg names, bypassing the identity
    transform). An ``output_schema`` is advertised and every result validated against it,
    raising loudly on any mismatch.

    ONE ``fixed_kwargs`` key is special: the ``payload_arg`` itself. Baking it lets the
    author supply partial DEFAULTS for the payload rather than an absolute constant that
    would discard the caller's whole validated object. Such a baked default MUST be a JSON
    object (dict) — a non-dict is a LOUD authoring error raised HERE at bind time (so the
    dry-run bake surfaces it as a 400 at save, not on the first call). At call time the
    caller's object deep-merges OVER the baked defaults, caller-wins-per-key
    (:func:`deep_merge`); the merged object — what the base tool actually sees — is what is
    validated and forwarded. Every OTHER ``fixed_kwargs`` key stays an absolute hidden
    constant.
    """
    support = app.presets.input_schema_support(base_tool)
    if support is None:
        raise ValueError(
            f"base tool {base_tool!r} does not accept a preset input_schema "
            "(no PresetInputSchemaSupport is registered for it)"
        )
    payload_arg = support.payload_arg

    # When the preset bakes the PAYLOAD ARG itself, the baked value is a set of partial
    # DEFAULTS the caller deep-merges over — so it MUST be a JSON object. Reject a non-dict
    # LOUDLY at bind time: reachable through the authoring dry-run bake, this fails at save
    # (a 400) rather than deferring an un-mergeable constant to the first call.
    baked_payload_present = payload_arg in fixed_kwargs
    baked_payload_defaults: dict[str, Any] = {}
    if baked_payload_present:
        raw_baked_payload = fixed_kwargs[payload_arg]
        if not isinstance(raw_baked_payload, dict):
            raise ValueError(
                f"preset {name!r} bakes a non-dict default for the payload argument "
                f"{payload_arg!r} while declaring an input_schema (got "
                f"{type(raw_baked_payload).__name__}); a baked payload default must be a "
                "JSON object so the caller's fields can deep-merge over it"
            )
        baked_payload_defaults = raw_baked_payload

    async def _route(**kwargs: Any) -> Any:
        # A custom transform_fn bypasses FastMCP's own argument validation, so the kernel
        # validates against the authored input_schema HERE before forwarding, re-raising a
        # mismatch as FastMCP's ValidationError so it surfaces as a client error (logged as
        # a warning, reaching the caller unmasked) rather than a masked 500.
        caller_object = dict(kwargs)
        # Step 1: validate the caller's OWN object. A caller mistake (missing-required,
        # wrong-typed, or additionalProperties-forbidden field) surfaces VERBATIM as a
        # client error, never routed raw into ``payload_arg``.
        try:
            validate_against_json_schema(caller_object, input_schema)
        except JsonSchemaValidationError as exc:
            raise FastMCPValidationError(f"caller input does not match the preset's input_schema: {exc}") from exc
        if baked_payload_present:
            # The baked payload is a set of DEFAULTS: deep-merge the caller OVER it,
            # caller-wins-per-key. The merged object is what the base tool actually sees.
            merged = deep_merge(baked_payload_defaults, caller_object)
            # Step 2: validate the MERGED object. The caller already cleared step 1, so a
            # fresh violation here is induced by the preset's baked defaults — attribute it
            # to the preset by name (the caller's own object alone would have passed).
            try:
                validate_against_json_schema(merged, input_schema)
            except JsonSchemaValidationError as exc:
                raise FastMCPValidationError(
                    f"preset {name!r}'s baked payload defaults produce an input that does not "
                    f"match its input_schema: {exc}"
                ) from exc
            # Forward the MERGED payload under ``payload_arg``; spread ``fixed_kwargs`` first
            # so the merged value overrides the baked payload key while EVERY other baked
            # kwarg stays its absolute hidden constant.
            result = await forward_raw(**{**fixed_kwargs, payload_arg: merged})
        else:
            result = await forward_raw(**{payload_arg: caller_object, **fixed_kwargs})
        # A park sentinel is a SUSPEND signal, not the tool's output: a suspending
        # call parked the caller and the dispatch's reveal gate stowed the
        # ``SuspendedInteraction`` RAW. Output-schema validation must NOT apply to
        # the park — recognized on the gate by TYPE, the flattened result passes
        # through so the dispatch returns the sentinel intact; the real result is
        # validated on resume. Non-parking results are still validated below.
        has_park, park = stowed_park()
        if has_park and isinstance(park, SuspendedInteraction):
            return result
        if output_schema is not None:
            validate_against_json_schema(result.structured_content, output_schema)
        return result

    # When the author declares NO output_schema, advertise the BASE tool's own output
    # schema (its typed result — e.g. ``ExecResult``) so the exposed preset reconstructs to
    # the same typed value the base does, matching the plain-preset path (which inherits it
    # via FastMCP's fallback). A custom transform_fn otherwise defaults the exposed schema to
    # nothing, leaving the caller a raw dict. The base's own result already conforms, so this
    # is advertisement only — ``_route`` validates against the AUTHORED schema alone.
    advertised_output_schema = output_schema if output_schema is not None else base.output_schema
    tool = Tool.from_tool(
        base,
        name=name,
        description=description,
        output_schema=advertised_output_schema,
        transform_fn=_route,
    )
    # The exposed tool advertises the AUTHORED schema as its input contract. FastMCP does
    # NOT validate the caller against ``.parameters`` for a custom transform_fn, so ``_route``
    # validates the caller's object against ``input_schema`` itself and packs the validated
    # object into ``payload_arg``. The base tool's own schema keys are supplied by the baked
    # ``fixed_kwargs`` (the remaining non-``payload_arg`` keys) inside ``_route``.
    tool.parameters = input_schema
    return tool
