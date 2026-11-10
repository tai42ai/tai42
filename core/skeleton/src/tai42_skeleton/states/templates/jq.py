"""The upload compile of a template's all-inline ``template_jq`` section."""

from __future__ import annotations

from collections.abc import Mapping

from tai42_contract.states.errors import TemplateValidationError
from tai42_contract.states.models import StateTemplateJq
from tai42_kit.states.template_jq import program_variables, template_jq_prelude
from tai42_kit.utils.data.jq_errors import JqProgramError
from tai42_kit.utils.data.jq_util import compile_check


async def _compile_template_jq_inline(programs: Mapping[str, StateTemplateJq]) -> None:
    """Compile every ``template_jq`` program at upload when EVERY body is inline.

    Inline everywhere is the point the full sibling prelude is known without a fetch. A
    by-id body anywhere in the section defers the whole section's compile to the save door
    (:meth:`~tai42_skeleton.states.service.StatesService._compile_by_id_template_jq`), the
    point that can render the stored resources; nothing is silently skipped. Each program
    compiles over the sibling INPUT-purpose prelude with its purpose's variables predeclared
    (``$parameters``/``$declarations``, and ``$params`` for an input program or ``$input`` for
    an update program); a reference cycle among input programs (which jq cannot express) fails
    the compile loudly.
    """
    if not all(p.jq.content is not None for p in programs.values()):
        return
    rendered_inputs = {name: p.jq.content for name, p in programs.items() if p.purpose == "input" and p.jq.content}
    prelude = template_jq_prelude(rendered_inputs)
    for name, program in programs.items():
        if program.jq.content is None:
            raise AssertionError
        try:
            await compile_check(prelude + program.jq.content, variables=program_variables(program.purpose))
        except JqProgramError as exc:
            raise TemplateValidationError(f"template_jq {name!r} jq is not a valid jq expression: {exc}") from exc
