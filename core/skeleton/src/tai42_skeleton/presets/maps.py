"""The preset map rules, shared by the bind kernel and the document check.

An input map needs an input schema (it maps what the caller sends, which the schema describes)
and may read the stored fixed kwargs as ``$fixed``; an output map reads only the result.
"""

from __future__ import annotations

from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import compile_check
from tai42_kit.utils.render import resolve_text_body

INPUT_MAP_VARIABLES: tuple[str, ...] = ("fixed",)
OUTPUT_MAP_VARIABLES: tuple[str, ...] = ()


class PresetMapError(ValueError):
    """A preset's map is refused: an input map without an input schema, or a map that does not compile."""


async def resolve_preset_maps(
    name: str,
    body_input_map: TemplatedText | str | None,
    body_output_map: TemplatedText | str | None,
    *,
    has_input_schema: bool,
) -> tuple[str | None, str | None]:
    """Resolve the preset ``name``'s two maps to their jq texts and prove each compiles.

    A text that cannot be rendered or is empty raises
    :class:`~tai42_kit.utils.render.TextBodyError`; an input map without an input schema and a
    map that does not compile raise :class:`PresetMapError`.
    """
    input_map = await resolve_text_body(f"preset {name!r} input_map", body_input_map)
    output_map = await resolve_text_body(f"preset {name!r} output_map", body_output_map)
    if input_map is not None:
        if not has_input_schema:
            raise PresetMapError("an input map needs an input schema that describes what the caller sends")
        try:
            compile_check(input_map, variables=INPUT_MAP_VARIABLES)
        except ValueError as exc:
            raise PresetMapError(f"input_map does not compile: {exc}") from exc
    if output_map is not None:
        try:
            compile_check(output_map, variables=OUTPUT_MAP_VARIABLES)
        except ValueError as exc:
            raise PresetMapError(f"output_map does not compile: {exc}") from exc
    return input_map, output_map
