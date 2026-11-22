"""The one wording of a ``template_jq`` program that does not compile."""

from __future__ import annotations

from tai42_kit.states.template_jq import TemplateJqCompileError
from tai42_kit.utils.data.jq_errors import JqProgramError

from tai42_skeleton.states.templates.jq import template_jq_refusal


def test_the_refusal_names_the_program_and_carries_jq_s_message() -> None:
    exc = TemplateJqCompileError("peek", JqProgramError("jq: error: syntax error"))
    assert template_jq_refusal(exc) == "template_jq 'peek' is not a valid jq expression: jq: error: syntax error"
