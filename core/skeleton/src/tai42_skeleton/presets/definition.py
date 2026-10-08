"""The preset document's save rules: the checks whose verdict depends on the preset body alone.

Every preset write door and the versioned-document restore run :func:`check_preset_body`. It
judges the document (its name, its schemas, its state binding) against the stores a restore
fills before it (templates, states), never against the live process registries (tools, agents,
extensions, the presets' per-base registries): those rules stay at the doors, and a restored
preset whose base the live registry cannot bind is quarantined by the preset rehydrate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import PresetBody
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.json_schema_util import InvalidJsonSchemaError, check_json_schema
from tai42_kit.utils.render import SchemaBodyError, resolve_schema_body

from tai42_skeleton.extensions.registry import extension_name
from tai42_skeleton.presets.manager import is_valid_preset_name
from tai42_skeleton.versioning.restore_checks import register_restore_check

# The versioned-document kind a preset is stored under.
_PRESET_KIND = "preset"


class PresetDefinitionError(ValueError):
    """A preset body the document rules refuse; the message is the refusal a door answers with."""


async def output_schema_body_error(
    output_schema: TemplatedText | dict[str, Any] | None,
    extensions: Sequence[Sequence[ExtensionElement]],
) -> str | None:
    """The first violation of an ``output_schema`` judged on the body alone, or ``None`` when unset or valid.

    Rejects, in order: a by-id schema whose stored resource cannot be rendered or does not render
    to a JSON object (the ``TemplatedText | dict`` union is resolved the same way the bake
    resolves it); a schema that fails the draft-2020-12 meta-schema; a non-object schema (both
    dispatch paths require an object root); a clash with an explicit ``output_schema`` extension
    entry (the shape declared in two places).
    """
    if output_schema is None:
        return None
    try:
        resolved = await resolve_schema_body("output_schema", output_schema)
    except SchemaBodyError as exc:
        return str(exc)
    if resolved is None:
        raise AssertionError
    try:
        check_json_schema(resolved)
    except InvalidJsonSchemaError as exc:
        return f"output_schema is not a valid JSON Schema: {exc}"
    if resolved.get("type") != "object":
        return 'output_schema must be an object schema ("type": "object")'
    for combo in extensions:
        for element in combo:
            if extension_name(element) == "output_schema":
                return (
                    "output_schema field conflicts with an explicit 'output_schema' extension entry; "
                    "declare the output shape in exactly one place"
                )
    return None


async def check_preset_body(body: PresetBody, *, name: str, attach: bool) -> None:
    """Run the preset document's save rules over ``body`` stored under ``name``.

    In order: the name law, the output-schema body rules, the input-schema resolution, then the
    state binding when the body carries one — validated and its templates attached
    (``attach=True``), or validated only (``attach=False``, the dry run). The document rules raise
    :class:`PresetDefinitionError`; the binding's own errors propagate as the binding validation
    raises them, for the caller to classify by type.
    """
    if not is_valid_preset_name(name):
        raise PresetDefinitionError(f"invalid preset name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$")
    schema_error = await output_schema_body_error(body.output_schema, body.extensions)
    if schema_error is not None:
        raise PresetDefinitionError(schema_error)
    try:
        await resolve_schema_body(f"preset {name!r} input_schema", body.input_schema)
    except SchemaBodyError as exc:
        raise PresetDefinitionError(str(exc)) from exc
    if isinstance(body.state_binding, StateBinding):
        from tai42_skeleton.app import instance
        from tai42_skeleton.tools import state_binding

        if attach:
            await state_binding.validate_and_attach_binding(instance.app, body.state_binding)
        else:
            await state_binding.validate_binding(instance.app, body.state_binding)


async def _restore_check(name: str, body: dict[str, Any]) -> None:
    """The preset kind's restore check: the document rules over the active body, attaching its binding."""
    await check_preset_body(PresetBody.model_validate(body), name=name, attach=True)


register_restore_check(_PRESET_KIND, _restore_check)


__all__ = ["PresetDefinitionError", "check_preset_body", "output_schema_body_error"]
