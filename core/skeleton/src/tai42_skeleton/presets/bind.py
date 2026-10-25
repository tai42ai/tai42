"""The preset bind kernel — the single point every preset builds its live tool through.

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
stored body every time. A preset's display identity is its own name and description:
it serves none of its base's ``title``, ``annotations.title`` or ``icons``. Its
behaviour hints are its base's (:func:`_hints_of`): a preset calls the base body with
some arguments fixed, and a hint describes that body. A preset carries NO native tags —
grouping is the tool_meta overlay's job.

Bake through the PROGRAMMATIC ``transform_args`` path (whose ``default`` accepts
any value, incl. dict/list) rather than a declarative scalar-only path, so a
non-scalar baked value is preserved.

A ``fixed_kwargs`` scalar leaf written ``!ENV ${VAR[:default]}`` is a secret
REFERENCE, not a stored credential: :func:`resolve_secret_refs` materialises it
from the process environment here, baking the resolved value while the store keeps
only the marker. A present var resolves to a ``SecretValue`` so a value that reaches
a recording door is masked;
:func:`~tai42_skeleton.tools.binding.secret_arguments.reveal_for_validation` then reveals a value sitting
under a TYPED string leaf of the base tool's schema (a ``token: str``, a ``list[str]``
element, a typed model field, a ``dict[str, str]`` value) to its plain form so it
passes the base tool's pydantic argument validation, while a value under a PERMISSIVE
leaf (``Any`` / ``object``) stays wrapped.

An ``input_map`` (a jq program, which requires an ``input_schema``) turns the caller's
validated object into the base tool's arguments; ``$fixed`` reads the preset's fixed kwargs
as stored, and the fixed kwargs are merged over the mapped arguments, never overridden by
them (:func:`merge_fixed`).

A preset's OUTPUT STAGE is its ``output_map`` (a jq program over the base's result) then
its ``output_schema`` (an object JSON Schema the stage's result must match), applied by
:func:`~tai42_skeleton.presets.output_stage.apply_output_stage_live` to every result that
is neither a park nor an error. An agent run base with an ``output_schema`` and no
``output_map`` runs no stage: the schema is baked into the run tool's ``response_format``
(a hidden, fixed constant; the contract's ``response_format`` rule requires a top-level
``title``, and the preset name — held to the preset-name law — is injected when absent) so
the agent FORCES a structured output, and the preset advertises the authored (title-free)
schema. The preset advertises the authored ``output_schema`` when one is set, nothing when an
``output_map`` reshapes the result without one, and the base's own schema otherwise.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform, forward, forward_raw
from tai42_contract.presets import PresetOutputStage
from tai42_contract.secrets import SecretValue
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.data.json_schema_util import (
    JsonSchemaValidationError,
    validate_against_json_schema,
)
from tai42_kit.utils.render import resolve_schema_body

from tai42_skeleton.presets.maps import resolve_preset_maps
from tai42_skeleton.presets.output_stage import apply_output_stage_live
from tai42_skeleton.presets.secret_refs import resolve_secret_refs
from tai42_skeleton.tools.binding.secret_arguments import reveal_for_validation
from tai42_skeleton.tools.binding.transformed_run import SchemaInputTool
from tai42_skeleton.tools.stage_frames import stage_frame

if TYPE_CHECKING:
    from mcp.types import ToolAnnotations
    from tai42_contract.app import TaiApp

logger = logging.getLogger(__name__)


async def preset_bind(
    app: TaiApp,
    base_tool: str,
    fixed_kwargs: dict[str, Any],
    *,
    name: str,
    description: str = "",
    output_schema: TemplatedText | dict[str, Any] | None = None,
    input_schema: TemplatedText | dict[str, Any] | None = None,
    input_map: TemplatedText | str | None = None,
    output_map: TemplatedText | str | None = None,
) -> Tool:
    """Return a FastMCP tool transform of ``base_tool`` as the new named tool ``name``.

    Resolves the base ``Tool`` object (``app.tools.get_tool`` — hence async), then
    builds the transform in one call. Each ``fixed_kwargs`` key becomes a hidden,
    fixed constant; the remaining arguments keep the base tool's typed schema. An
    ``input_map`` builds the arguments from the caller's object validated against the
    authored ``input_schema`` (:func:`_bind_mapped_input`). Without one, an ``input_schema``
    makes the exposed tool advertise the AUTHORED schema as its own input contract and routes
    the caller's validated object into the base tool's ``payload_arg`` (the base tool must have
    registered ``PresetInputSchemaSupport``; without it this is a loud error — the mechanism
    carries no base-tool knowledge). The output stage is described in the module docstring.

    Each schema and map is a ``TemplatedText`` union: this bind is the single point every
    preset builds its live tool through (the live rehydrate/reload AND the save-time dry-run
    bake), so it is where a by-id body is RESOLVED before use. An unfetchable stored id, a
    schema body that does not render to a JSON-object schema, a map that does not compile and
    an input map without an input schema fail LOUDLY here, so a bad body is a 400 at the
    save's dry-run bake, never a live tool bound on it.
    """
    output_schema = await resolve_schema_body(f"preset {name!r} output_schema", output_schema)
    input_schema = await resolve_schema_body(f"preset {name!r} input_schema", input_schema)
    input_map_text, output_map_text = await resolve_preset_maps(
        name, input_map, output_map, has_input_schema=input_schema is not None
    )
    base = await app.tools.get_tool(base_tool)
    # ``$fixed`` reads the fixed kwargs as stored: a secret reference stays its marker text.
    stored_fixed = copy.deepcopy(fixed_kwargs) if input_map_text is not None else {}
    # Materialise every ``!ENV ${VAR}`` secret reference from the process environment
    # once, here at the shared chokepoint, so every bind path bakes the resolved value —
    # a present var wrapped in ``SecretValue`` — while the STORE body keeps the marker
    # verbatim. An absent required var raises loudly (a 400 at the save dry-run, a
    # quarantine at rehydrate).
    fixed_kwargs = resolve_secret_refs(fixed_kwargs)
    # Reveal a resolved reference sitting under a TYPED string leaf of the base tool's
    # schema — a top-level ``token: str``, a ``list[str]`` element, a typed model field,
    # a ``dict[str, str]`` value — so it survives the base tool's pydantic argument
    # validation and reaches the tool body; a reference under a PERMISSIVE leaf (``Any``
    # / ``object``) stays wrapped so a value that reaches a recording door is masked.
    fixed_kwargs = reveal_for_validation(fixed_kwargs, base.parameters)

    is_agent = base_tool in app.agents.all_agents()
    # An agent base with a schema and no map forces the schema through ``response_format``
    # instead of running a stage.
    forced_schema = output_schema if is_agent and output_map_text is None else None
    stage: PresetOutputStage | None = None
    if output_map_text is not None or (output_schema is not None and forced_schema is None):
        stage = PresetOutputStage(preset=name, output_map=output_map_text, output_schema=output_schema)
    if output_schema is not None:
        advertised_output_schema = output_schema
    elif output_map_text is not None:
        advertised_output_schema = None
    else:
        advertised_output_schema = base.output_schema

    if input_map_text is not None:
        assert input_schema is not None  # noqa: S101 resolve_preset_maps refuses an input map without one
        return _bind_mapped_input(
            base,
            fixed_kwargs,
            stored_fixed,
            name=name,
            description=description,
            input_schema=input_schema,
            input_map=input_map_text,
            forced_schema=forced_schema,
            stage=stage,
            advertised_output_schema=advertised_output_schema,
        )
    if input_schema is not None:
        return _bind_with_input_schema(
            app,
            base,
            base_tool,
            fixed_kwargs,
            name=name,
            description=description,
            input_schema=input_schema,
            forced_schema=forced_schema,
            stage=stage,
            advertised_output_schema=advertised_output_schema,
        )

    transform_args = {key: ArgTransform(hide=True, default=value) for key, value in fixed_kwargs.items()}
    if stage is not None:
        return _bind_validated(base, name, description, transform_args, stage, advertised_output_schema)
    if forced_schema is not None:
        return _bind_agent_forced(base, name, description, transform_args, forced_schema)
    return _bind_plain(base, name, description, transform_args)


def _hints_of(base: Tool) -> ToolAnnotations | None:
    """The behaviour hints a preset over ``base`` serves: the base's annotations without their display title.

    ``None`` when the base declares none.
    """
    if base.annotations is None:
        return None
    return base.annotations.model_copy(update={"title": None})


def _forced_response_format(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """The ``response_format`` an agent base is forced with: ``schema`` with the preset name as its default title."""
    response_format = dict(schema)
    response_format.setdefault("title", name)
    return response_format


def _bind_plain(base: Tool, name: str, description: str, transform_args: dict[str, ArgTransform]) -> Tool:
    """Transform ``base`` baking the hidden constants, with NO output stage.

    The plain path (one ``Tool.from_tool``).
    """
    return Tool.from_tool(
        base,
        name=name,
        description=description,
        title=None,
        annotations=_hints_of(base),
        transform_args=transform_args,
    )


def _bind_agent_forced(
    base: Tool,
    name: str,
    description: str,
    transform_args: dict[str, ArgTransform],
    output_schema: dict[str, Any],
) -> Tool:
    """Force structured output by baking ``response_format`` from the authored schema.

    The advertised output schema stays the authored (title-free) value; the agent's own drain
    validates the forced result, so the build has no transform and no output stage.
    """
    transform_args["response_format"] = ArgTransform(hide=True, default=_forced_response_format(output_schema, name))
    return Tool.from_tool(
        base,
        name=name,
        description=description,
        title=None,
        annotations=_hints_of(base),
        transform_args=transform_args,
        output_schema=output_schema,
    )


def _bind_validated(
    base: Tool,
    name: str,
    description: str,
    transform_args: dict[str, ArgTransform],
    stage: PresetOutputStage,
    advertised_output_schema: dict[str, Any] | None,
) -> Tool:
    """Transform ``base`` baking the hidden constants and run ``stage`` over every result."""

    async def _apply_stage(**kwargs: Any) -> Any:
        with stage_frame(stage) as frame:
            result = await forward(**kwargs)
        return await apply_output_stage_live(frame, result)

    return Tool.from_tool(
        base,
        name=name,
        description=description,
        title=None,
        annotations=_hints_of(base),
        transform_args=transform_args,
        output_schema=advertised_output_schema,
        transform_fn=_apply_stage,
    )


def merge_fixed(mapped: dict[str, Any], fixed: dict[str, Any], *, preset: str) -> dict[str, Any]:
    """Merge the preset's ``fixed`` kwargs over the ``mapped`` arguments; a fixed value is never overridden.

    Recurses where both sides hold a dict at a key; any other key on both sides raises
    :class:`~fastmcp.exceptions.ToolError` naming its dotted path. Neither input is mutated:
    ``fixed`` is a bind's shared constants reused across every call, so a fixed-only subtree is
    copied into the result (:func:`_copy_baked_default`).
    """
    return _merge_fixed(mapped, fixed, preset=preset, path="")


def _merge_fixed(mapped: dict[str, Any], fixed: dict[str, Any], *, preset: str, path: str) -> dict[str, Any]:
    merged = dict(mapped)
    for key, fixed_value in fixed.items():
        dotted = f"{path}{key}"
        if key not in mapped:
            merged[key] = _copy_baked_default(fixed_value)
            continue
        mapped_value = mapped[key]
        if isinstance(mapped_value, dict) and isinstance(fixed_value, dict):
            merged[key] = _merge_fixed(mapped_value, fixed_value, preset=preset, path=f"{dotted}.")
            continue
        raise ToolError(
            f"preset {preset!r} input map sets {dotted}, which the preset fixes; a fixed value cannot be overridden"
        )
    return merged


def _validate_caller_object(caller_object: dict[str, Any], input_schema: dict[str, Any]) -> None:
    """Validate the caller's object against the authored ``input_schema``.

    A custom ``transform_fn`` bypasses FastMCP's own argument validation, so a mismatch is
    re-raised as FastMCP's ValidationError: a client error, reaching the caller unmasked.
    """
    try:
        validate_against_json_schema(caller_object, input_schema)
    except JsonSchemaValidationError as exc:
        raise FastMCPValidationError(f"caller input does not match the preset's input_schema: {exc}") from exc


def _bind_mapped_input(
    base: Tool,
    fixed_kwargs: dict[str, Any],
    stored_fixed: dict[str, Any],
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    input_map: str,
    forced_schema: dict[str, Any] | None,
    stage: PresetOutputStage | None,
    advertised_output_schema: dict[str, Any] | None,
) -> Tool:
    """Build the exposed tool whose arguments the ``input_map`` makes from the caller's object.

    The tool advertises the authored ``input_schema``; ``_route`` validates the caller's object
    against it, runs the map with ``$fixed`` bound to the stored fixed kwargs, merges the fixed
    kwargs over the mapped arguments (:func:`merge_fixed`) and forwards them under the base's own
    argument names; the base validates them. The map decides where the caller's data goes, so the
    base needs no ``PresetInputSchemaSupport``. A ``forced_schema`` (an agent base with a schema
    and no output map) is added as ``response_format``; a ``stage`` runs over the result.
    """
    forced_response_format = _forced_response_format(forced_schema, name) if forced_schema is not None else None

    async def _route(**kwargs: Any) -> Any:
        caller_object = dict(kwargs)
        _validate_caller_object(caller_object, input_schema)
        try:
            mapped = await run_jq_first(input_map, caller_object, variables={"fixed": stored_fixed})
        except (ValueError, TypeError, TimeoutError) as exc:
            # The jq text can quote ``$fixed`` or the caller's values: it stays in the server log.
            logger.warning("preset %r input map failed: %s", name, exc)
            raise ToolError(f"preset {name!r} input map failed") from None
        if not isinstance(mapped, dict):
            raise ToolError(
                f"preset {name!r} input map must produce an object of the base tool's arguments, "
                f"got {type(mapped).__name__}"
            )
        fixed = fixed_kwargs
        if forced_response_format is not None:
            fixed = {**fixed_kwargs, "response_format": forced_response_format}
        arguments = merge_fixed(mapped, fixed, preset=name)
        if stage is None:
            return await forward_raw(**arguments)
        with stage_frame(stage) as frame:
            result = await forward_raw(**arguments)
        return await apply_output_stage_live(frame, result)

    tool = SchemaInputTool.from_tool(
        base,
        name=name,
        description=description,
        title=None,
        annotations=_hints_of(base),
        output_schema=advertised_output_schema,
        transform_fn=_route,
    )
    tool.parameters = input_schema
    return tool


def deep_merge(baked: dict[str, Any], caller: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``caller`` over ``baked`` with CALLER-WINS-PER-KEY semantics.

    Recurses ONLY where both sides hold a dict.

    Applied per key:

    - key in both, both values dicts -> recurse (nested deep merge).
    - key in both, otherwise (scalars, lists, or mismatched types) -> the caller's
      value REPLACES the baked one (lists REPLACE, never concatenate; scalars are not
      coerced).
    - key only in ``baked`` -> the baked value fills the gap.
    - key only in ``caller`` -> the caller value passes through.

    Neither input is mutated; a fresh dict is returned. The result shares NO mutable state
    with ``baked``: a baked-only subtree (one no caller key overrides) is DEEP-COPIED into
    the result, not aliased — save an immutable :class:`~tai42_contract.secrets.SecretValue`
    leaf, which is aliased (it refuses deepcopy by design and cannot be mutated), its
    enclosing containers still copied. ``baked`` is a bind's shared payload defaults reused
    across every call, so aliasing a mutable subtree would let a consumer that mutates the
    forwarded merged payload poison those defaults for all later calls. Caller-supplied
    values, by contrast, come from a fresh per-call object and are taken by reference (a
    caller never shares state across calls).
    """
    # Baked-only keys are the sole aliasing seam (keys in ``caller`` are overwritten in the
    # loop below by either a recursion result or the caller's own value): deep-copy their
    # values so the returned object owns them and can never reach back into the shared defaults.
    merged: dict[str, Any] = {
        key: (value if key in caller else _copy_baked_default(value)) for key, value in baked.items()
    }
    for key, caller_value in caller.items():
        baked_value = baked.get(key)
        if key in baked and isinstance(baked_value, dict) and isinstance(caller_value, dict):
            merged[key] = deep_merge(baked_value, caller_value)
        else:
            merged[key] = caller_value
    return merged


def _copy_baked_default(value: Any) -> Any:
    """Deep-copy a baked-only value while ALIASING immutable ``SecretValue`` leaves.

    :func:`deep_merge` deep-copies a baked-only subtree so a consumer mutating the merged
    payload can never poison the bind's shared defaults. A :class:`SecretValue` refuses
    deepcopy (its ``__reduce__`` raises) and is immutable, so it is aliased in place; its
    enclosing dict/list containers are still copied, keeping every mutable subtree private
    to the returned object.
    """
    if isinstance(value, SecretValue):
        return value
    if isinstance(value, dict):
        return {key: _copy_baked_default(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_baked_default(item) for item in value]
    return copy.deepcopy(value)


def _bind_with_input_schema(
    app: TaiApp,
    base: Tool,
    base_tool: str,
    fixed_kwargs: dict[str, Any],
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    forced_schema: dict[str, Any] | None,
    stage: PresetOutputStage | None,
    advertised_output_schema: dict[str, Any] | None,
) -> Tool:
    """Build the exposed tool whose OWN advertised input schema is the authored ``input_schema``.

    Validates the caller's object against it and routes the validated object into the base tool's
    ``payload_arg``.

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
    transform). A ``forced_schema`` (an agent base with a schema and no output map) is added
    as ``response_format``; a ``stage`` runs over the result.

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
    if forced_schema is not None:
        fixed_kwargs = {**fixed_kwargs, "response_format": _forced_response_format(forced_schema, name)}

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
        caller_object = dict(kwargs)
        # Step 1: validate the caller's OWN object, so a caller mistake surfaces verbatim.
        _validate_caller_object(caller_object, input_schema)
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
            # Spread ``fixed_kwargs`` first so the merged value overrides the baked payload
            # key while EVERY other baked kwarg stays its absolute hidden constant.
            arguments = {**fixed_kwargs, payload_arg: merged}
        else:
            arguments = {payload_arg: caller_object, **fixed_kwargs}
        if stage is None:
            return await forward_raw(**arguments)
        with stage_frame(stage) as frame:
            result = await forward_raw(**arguments)
        return await apply_output_stage_live(frame, result)

    tool = SchemaInputTool.from_tool(
        base,
        name=name,
        description=description,
        title=None,
        annotations=_hints_of(base),
        output_schema=advertised_output_schema,
        transform_fn=_route,
    )
    # FastMCP does NOT validate the caller against ``.parameters`` for a custom
    # transform_fn, so ``_route`` validates the caller's object itself.
    tool.parameters = input_schema
    return tool
