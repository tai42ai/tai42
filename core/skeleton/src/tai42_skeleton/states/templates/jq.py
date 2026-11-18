"""The upload compile of a template's all-inline ``template_jq`` section."""

from __future__ import annotations

from collections.abc import Mapping

from tai42_contract.states.errors import TemplateValidationError
from tai42_contract.states.models import StateTemplateJq
from tai42_kit.states.template_jq import TemplateJqCompileError, compile_template_jq


async def compile_template_jq_inline(programs: Mapping[str, StateTemplateJq]) -> None:
    """Compile every ``template_jq`` program at upload when EVERY body is inline.

    Inline everywhere is the point the full sibling prelude is known without a fetch. A
    by-id body anywhere in the section defers the whole section's compile to the save door
    (:meth:`~tai42_skeleton.states.service.StatesService._compile_by_id_template_jq`), the
    point that can render the stored resources; nothing is silently skipped. The section
    compiles by the kit's rule (:func:`~tai42_kit.states.template_jq.compile_template_jq`).
    """
    bodies = {name: p.jq.content for name, p in programs.items() if p.jq.content is not None}
    if len(bodies) < len(programs):
        return
    try:
        await compile_template_jq(programs, bodies)
    except TemplateJqCompileError as exc:
        raise TemplateValidationError(f"template_jq {exc.program!r} jq is not a valid jq expression: {exc}") from exc
