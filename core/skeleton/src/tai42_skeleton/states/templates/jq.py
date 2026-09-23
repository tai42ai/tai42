"""The ``template_jq`` sibling-prelude builder and the inline compile check.

A template's INPUT-purpose programs are exposed to each other (and to update programs) as
``def tjq_<name>($params): <jq>;`` declarations; this module builds that dependency-first
prelude from rendered bodies and compiles an all-inline section at upload.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from tai42_contract.states.errors import TemplateValidationError
from tai42_contract.states.models import StateTemplateJq
from tai42_kit.utils.data.jq_util import compile_check

# The named jq variables a ``template_jq`` program body may reference beyond its own
# params: the attachment's effective ``$parameters`` and its ``$declarations``, bound at
# the evaluator seam that runs the program. Declared here so a program compiles at upload
# and every evaluator binds the same set.
MEMBER_JQ_VARIABLES: tuple[str, ...] = ("parameters", "declarations")


def _input_def(name: str, jq: str) -> str:
    """One sibling def ``def tjq_<name>($params): <jq>;`` an input program may call as ``tjq_<name>({…})``.

    Always arity one, the declared params delivered as the single ``$params`` object
    (``{}`` when the program declares none). ``jq`` is the input program's RENDERED body
    text.
    """
    return f"def tjq_{name}($params): {jq}; "


def _references(expr: str, name: str) -> bool:
    """Whether ``expr`` names ``name`` as a jq token (a call or a bare reference)."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", expr) is not None


def _input_order(rendered_inputs: Mapping[str, str]) -> list[str]:
    """A deterministic dependency-first ordering of input-program names.

    Keyed on each program's RENDERED body text: a program that references a sibling is
    emitted AFTER it (jq resolves names backward only). A reference cycle — which jq cannot
    express — falls back to sorted order, so the compile fails loudly at validation rather
    than silently.
    """
    names = sorted(rendered_inputs)
    # A sibling is called by its prelude def name ``tjq_<name>``; a def that calls another
    # must be emitted AFTER it (jq resolves names backward only).
    deps = {a: {b for b in names if b != a and _references(rendered_inputs[a], f"tjq_{b}")} for a in names}
    ordered: list[str] = []
    remaining = set(names)
    while remaining:
        ready = sorted(n for n in remaining if deps[n] <= set(ordered))
        if not ready:
            ordered.extend(sorted(remaining))
            break
        ordered.append(ready[0])
        remaining.discard(ready[0])
    return ordered


def template_jq_prelude(rendered_inputs: Mapping[str, str]) -> str:
    """The dependency-first run of ``def tjq_<name>($params): <jq>;`` sibling declarations.

    Built from the RENDERED body text of the template's INPUT-purpose programs (``name ->
    rendered jq``) — prepended to a program's jq at compile and at evaluation so a
    reference to a sibling input program resolves. Empty when the template declares no
    input programs. The bodies are rendered at the point of use (the save door and the
    evaluator seam); this function is pure over already-rendered text.
    """
    return "".join(_input_def(name, rendered_inputs[name]) for name in _input_order(rendered_inputs))


def _compile_template_jq_inline(programs: Mapping[str, StateTemplateJq]) -> None:
    """Compile every ``template_jq`` program at upload when EVERY body is inline.

    Inline everywhere is the point the full sibling prelude is known without a fetch. A
    by-id body anywhere in the section defers the whole section's compile to the save door
    (:meth:`~tai42_skeleton.states.service.StatesService._compile_by_id_template_jq`), the
    point that can render the stored resources; nothing is silently skipped. Each program
    compiles over the sibling INPUT-purpose prelude, with ``$parameters``/``$declarations``
    bound and — for an input program — the single ``$params`` object predeclared; a
    reference cycle among input programs (which jq cannot express) fails the compile loudly.
    """
    if not all(p.jq.content is not None for p in programs.values()):
        return
    rendered_inputs = {name: p.jq.content for name, p in programs.items() if p.purpose == "input" and p.jq.content}
    prelude = template_jq_prelude(rendered_inputs)
    for name, program in programs.items():
        if program.jq.content is None:
            raise AssertionError
        # An input program's declared params ride the SINGLE ``$params`` object, never
        # individual ``$<name>`` args; an update program reads the record subtree as ``.`` with
        # the adapter's input bound as ``$input``, and declares its ``$input`` keys as ``params``
        # (validated at apply, not bound here).
        variables = (*MEMBER_JQ_VARIABLES, "params") if program.purpose == "input" else (*MEMBER_JQ_VARIABLES, "input")
        try:
            compile_check(prelude + program.jq.content, variables=variables)
        except Exception as exc:
            raise TemplateValidationError(f"template_jq {name!r} jq is not a valid jq expression: {exc}") from exc
