"""What a state template's ``template_jq`` program gives, once it is resolved and rendered.

The module picks a program by name and purpose among the candidate templates, builds the
template's sibling input-program prelude, checks a call against the program's declared
``params``, reads the record document at the attachment path, evaluates the program and, for an
``update`` program, checks the op batch and rebases each op under the attachment path. How a
caller finds the candidates, renders a template and reads the record is the caller's.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from tai42_contract.states import StateTemplateJq
from tai42_contract.states.errors import StateNotFoundError, ValueValidationError

from tai42_kit.utils.data.jq_errors import JqProgramError
from tai42_kit.utils.data.jq_util import run_jq_first

# The named jq variables a ``template_jq`` program body may reference beyond its own call
# input: the attachment's effective ``$parameters`` and its ``$declarations``.
MEMBER_JQ_VARIABLES: tuple[str, ...] = ("parameters", "declarations")


def program_variables(purpose: Literal["input", "update"]) -> tuple[str, ...]:
    """The jq variable names a program of ``purpose`` is compiled and evaluated with.

    An input program's declared params ride the single ``$params`` object; an update program
    reads the adapter's input as ``$input``.
    """
    return (*MEMBER_JQ_VARIABLES, "params") if purpose == "input" else (*MEMBER_JQ_VARIABLES, "input")


def _input_def(name: str, jq: str) -> str:
    """One sibling def ``def tjq_<name>($params): <jq>;`` an input program may call as ``tjq_<name>({…})``.

    Always arity one, the declared params delivered as the single ``$params`` object
    (``{}`` when the program declares none). ``jq`` is the input program's RENDERED body
    text.
    """
    return f"def tjq_{name}($params): {jq}; "


# A sibling call ``tjq_<name>`` as a whole jq token; one tokenising pass per body finds every one.
_SIBLING_CALL_RE = re.compile(r"(?<![A-Za-z0-9_])tjq_([A-Za-z0-9_]+)")


def input_order(rendered_inputs: Mapping[str, str]) -> list[str]:
    """A deterministic dependency-first ordering of input-program names.

    Keyed on each program's RENDERED body text: a program that calls a sibling as ``tjq_<name>``
    is emitted AFTER it (jq resolves names backward only); among the programs ready at a step the
    smallest name goes first. A reference cycle — which jq cannot express — appends the remaining
    names sorted, so the compile fails loudly at validation rather than silently.
    """
    siblings = set(rendered_inputs)
    dependents: dict[str, list[str]] = {name: [] for name in siblings}
    pending: dict[str, int] = {}
    for name, body in rendered_inputs.items():
        refs = (set(_SIBLING_CALL_RE.findall(body)) & siblings) - {name}
        pending[name] = len(refs)
        for ref in refs:
            dependents[ref].append(name)
    ready = [name for name, count in pending.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        ordered.append(name)
        for dependent in dependents[name]:
            pending[dependent] -= 1
            if pending[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) < len(siblings):
        ordered.extend(sorted(siblings - set(ordered)))
    return ordered


def sibling_prelude(rendered_inputs: Mapping[str, str], order: Sequence[str]) -> str:
    """The ``def tjq_<name>($params): <jq>;`` sibling declarations in ``order`` (dependency-first)."""
    return "".join(_input_def(name, rendered_inputs[name]) for name in order)


def template_jq_prelude(rendered_inputs: Mapping[str, str]) -> str:
    """The dependency-first run of ``def tjq_<name>($params): <jq>;`` sibling declarations.

    Built from the RENDERED body text of the template's INPUT-purpose programs (``name ->
    rendered jq``) — prepended to a program's jq at compile and at evaluation so a
    reference to a sibling input program resolves. Empty when the template declares no
    input programs. Pure over already-rendered text.
    """
    return sibling_prelude(rendered_inputs, input_order(rendered_inputs))


def select_template_jq(
    state: str, name: str, purpose: str, candidates: Sequence[tuple[str, Mapping[str, StateTemplateJq]]]
) -> tuple[int, str]:
    """Pick the program a ``template_jq`` reference ``name`` names among ``candidates``.

    ``candidates`` are ``(template name, its programs by name)`` pairs; returns ``(index of the
    declaring candidate, program name)``. A QUALIFIED ``<template>.<name>`` picks that template's
    program — a template not among the candidates, or one that declares no such program, is a
    :class:`StateNotFoundError`. An UNQUALIFIED name picks the one candidate that declares it —
    none is a :class:`StateNotFoundError`, two or more a :class:`ValueValidationError` (the caller
    qualifies it). A program whose purpose is not ``purpose`` is a :class:`ValueValidationError`.
    """
    if "." in name:
        template_name, program_name = name.split(".", 1)
        matches = [i for i, (candidate, _programs) in enumerate(candidates) if candidate == template_name]
        if not matches:
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        if program_name not in candidates[matches[0]][1]:
            raise StateNotFoundError(
                f"template {template_name!r} attached on state {state!r} declares no template_jq {program_name!r}"
            )
    else:
        program_name = name
        matches = [i for i, (_candidate, programs) in enumerate(candidates) if name in programs]
        if not matches:
            raise StateNotFoundError(f"no template_jq {name!r} on any template attached on state {state!r}")
        if len(matches) > 1:
            templates = ", ".join(sorted(candidates[i][0] for i in matches))
            raise ValueValidationError(
                f"template_jq {name!r} is declared by more than one template attached on state {state!r} "
                f"({templates}); qualify it as <template>.{name}"
            )
    index = matches[0]
    actual = candidates[index][1][program_name].purpose
    if actual != purpose:
        raise ValueValidationError(
            f"template_jq {program_name!r} on template {candidates[index][0]!r} has purpose {actual!r}, "
            f"needs {purpose!r}"
        )
    return index, program_name


def record_subtree(data: Mapping[str, Any] | None, path: Sequence[str]) -> dict[str, Any]:
    """The record document at an attachment's ``path`` — the subtree a template's jq programs operate over.

    ``.`` of a program. No record (``None``), an absent node or a non-object node reads as ``{}``.
    """
    node: Any = data
    for seg in path:
        if not isinstance(node, Mapping):
            return {}
        node = node.get(seg)
    return dict(node) if isinstance(node, Mapping) else {}


def rebase_op(op: Any, path: Sequence[str]) -> dict[str, Any]:
    """One template-relative op with its ``path`` rebased under the attachment ``path``.

    So an update program (like a fill) authors its ops in its own coordinates. A
    malformed op is a loud refusal.
    """
    if not isinstance(op, dict) or not isinstance(op.get("path"), list):
        raise ValueValidationError(f"an update-program op must be an object carrying a list path, got {op!r}")
    return {**op, "path": [*path, *op["path"]]}


@dataclass(frozen=True, slots=True)
class BoundProgram:
    """A resolved ``template_jq`` program bound to one attachment: what its evaluation needs and nothing else."""

    template: str
    """The declaring template's name."""
    name: str
    """The program's name."""
    params: list[str]
    """The program's declared params, shown as this list in a refusal."""
    body: str
    """The program's RENDERED jq."""
    prelude: str
    """The template's sibling input-program prelude."""
    path: list[str]
    """The attachment path."""
    parameters: Mapping[str, Any]
    """The attachment's effective parameters, bound as ``$parameters``."""
    declarations: Mapping[str, Any]
    """The attachment's declarations, bound as ``$declarations``."""


# The caller's read of the subject's record document; ``None`` when the subject has no record.
RecordData = Callable[[], Awaitable[Mapping[str, Any] | None]]


async def evaluate_input_program(program: BoundProgram, args: Mapping[str, Any], record: RecordData) -> Any:
    """The value an ``input`` program gives for ``args`` over the subject's record.

    ``args`` carries exactly the declared ``params`` (a value may be null), bound as the single
    ``$params`` object; a missing or undeclared key is a :class:`ValueValidationError` raised before
    the record is read. The jq runs over the record's subtree at the attachment path with
    ``$parameters``, ``$declarations`` and ``$params`` bound and the sibling prelude prepended.
    """
    missing = sorted(set(program.params) - set(args))
    unknown = sorted(set(args) - set(program.params))
    if missing or unknown:
        raise ValueValidationError(
            f"template_jq {program.name!r} on template {program.template!r} takes params {program.params}"
            + (f", missing {missing}" if missing else "")
            + (f", got unknown {unknown}" if unknown else "")
        )
    subtree = record_subtree(await record(), program.path)
    variables: dict[str, Any] = {
        "parameters": program.parameters,
        "declarations": program.declarations,
        "params": dict(args),
    }
    return await _evaluate(program, subtree, variables)


async def derive_update_ops(program: BoundProgram, input_: Any, record: RecordData) -> list[dict[str, Any]]:
    """The op batch an ``update`` program authors for ``input_``, rebased under the attachment path.

    When the program DECLARES ``params`` they are the contract for its input: ``input_`` must be an
    object carrying exactly those keys (a value may be null) — a non-object, a missing or an
    undeclared key is a :class:`ValueValidationError` raised before the record is read; a program
    that declares none takes any ``input_``. The jq runs over the record's subtree at the
    attachment path with ``$parameters``, ``$declarations`` and ``$input`` bound and the sibling
    prelude prepended; a result that is not a list, or an op that is not an object carrying a list
    ``path``, is a :class:`ValueValidationError`.
    """
    if program.params:
        if not isinstance(input_, dict):
            raise ValueValidationError(
                f"template_jq {program.name!r} on template {program.template!r} declares params "
                f"{program.params}, so its input must be an object, got {type(input_).__name__}"
            )
        missing = sorted(set(program.params) - set(input_))
        unknown = sorted(set(input_) - set(program.params))
        if missing or unknown:
            raise ValueValidationError(
                f"template_jq {program.name!r} on template {program.template!r} declares params {program.params}"
                + (f", input missing {missing}" if missing else "")
                + (f", input has undeclared {unknown}" if unknown else "")
            )
    subtree = record_subtree(await record(), program.path)
    variables: dict[str, Any] = {
        "parameters": program.parameters,
        "declarations": program.declarations,
        "input": input_,
    }
    result = await _evaluate(program, subtree, variables)
    if not isinstance(result, list):
        raise ValueValidationError(
            f"template_jq {program.name!r} on template {program.template!r} is an update program, so its jq must "
            f"return an op batch (a list of ops), got {type(result).__name__}"
        )
    return [rebase_op(op, program.path) for op in result]


async def _evaluate(program: BoundProgram, subtree: dict[str, Any], variables: dict[str, Any]) -> Any:
    """``program``'s first value over ``subtree``; a :class:`JqProgramError` is the author's refusal naming it.

    Any other fault of the run (the compute pool's) propagates unwrapped.
    """
    try:
        return await run_jq_first(program.body, subtree, prelude=program.prelude, variables=variables)
    except JqProgramError as exc:
        raise ValueValidationError(
            f"template_jq {program.name!r} on template {program.template!r} failed to evaluate: {exc}"
        ) from exc
