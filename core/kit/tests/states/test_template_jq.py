"""The ``template_jq`` program semantics: selection, the call checks, the subtree, the evaluation and the op shaping."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

pytest.importorskip("jq")

from tai42_contract.states import StateTemplateJq
from tai42_contract.states.errors import StateNotFoundError, ValueValidationError

from tai42_kit.compute import ComputeWorkerLostError
from tai42_kit.states import template_jq as template_jq_mod
from tai42_kit.states.template_jq import (
    BoundProgram,
    derive_update_ops,
    evaluate_input_program,
    select_template_jq,
    template_jq_prelude,
)
from tai42_kit.utils.data.jq_errors import JqProgramError

_STATE = "status"


def _program(purpose: str, jq: str = ".", params: list[str] | None = None) -> StateTemplateJq:
    return StateTemplateJq.model_validate({"purpose": purpose, "jq": {"content": jq}, "params": params or []})


def _bound(
    body: str, *, params: list[str] | None = None, prelude: str = "", path: list[str] | None = None
) -> BoundProgram:
    return BoundProgram(
        template="probe",
        name="peek",
        params=params or [],
        body=body,
        prelude=prelude,
        path=["probe"] if path is None else path,
        parameters={"limit": 3},
        declarations={"allowed": ["a"]},
    )


class _Record:
    """The subject's record document, counting each read."""

    def __init__(self, data: Mapping[str, Any] | None) -> None:
        self.data = data
        self.reads = 0

    async def __call__(self) -> Mapping[str, Any] | None:
        self.reads += 1
        return self.data


_CANDIDATES = [
    ("alpha", {"count": _program("input"), "add": _program("update")}),
    ("beta", {"count": _program("input"), "peek": _program("input")}),
]


# --------------------------------------------------------------------------- #
# selection                                                                    #
# --------------------------------------------------------------------------- #
def test_a_qualified_name_on_a_template_not_among_the_candidates_is_not_found() -> None:
    with pytest.raises(StateNotFoundError) as excinfo:
        select_template_jq(_STATE, "gamma.count", "input", _CANDIDATES)
    assert str(excinfo.value) == "template 'gamma' is not attached on state 'status'"


def test_a_qualified_name_the_template_does_not_declare_is_not_found() -> None:
    with pytest.raises(StateNotFoundError) as excinfo:
        select_template_jq(_STATE, "alpha.peek", "input", _CANDIDATES)
    assert str(excinfo.value) == "template 'alpha' attached on state 'status' declares no template_jq 'peek'"
    assert select_template_jq(_STATE, "beta.count", "input", _CANDIDATES) == (1, "count")


def test_an_unqualified_name_no_candidate_declares_is_not_found() -> None:
    with pytest.raises(StateNotFoundError) as excinfo:
        select_template_jq(_STATE, "total", "input", _CANDIDATES)
    assert str(excinfo.value) == "no template_jq 'total' on any template attached on state 'status'"
    assert select_template_jq(_STATE, "peek", "input", _CANDIDATES) == (1, "peek")


def test_an_unqualified_name_two_candidates_declare_must_be_qualified() -> None:
    with pytest.raises(ValueValidationError) as excinfo:
        select_template_jq(_STATE, "count", "input", list(reversed(_CANDIDATES)))
    assert str(excinfo.value) == (
        "template_jq 'count' is declared by more than one template attached on state 'status' "
        "(alpha, beta); qualify it as <template>.count"
    )


def test_a_program_of_the_other_purpose_is_refused() -> None:
    with pytest.raises(ValueValidationError) as excinfo:
        select_template_jq(_STATE, "alpha.add", "input", _CANDIDATES)
    assert str(excinfo.value) == "template_jq 'add' on template 'alpha' has purpose 'update', needs 'input'"
    with pytest.raises(ValueValidationError) as excinfo:
        select_template_jq(_STATE, "peek", "update", _CANDIDATES)
    assert str(excinfo.value) == "template_jq 'peek' on template 'beta' has purpose 'input', needs 'update'"


# --------------------------------------------------------------------------- #
# input programs                                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "template_jq 'peek' on template 'probe' takes params ['x'], missing ['x']"),
        ({"x": 1, "y": 2}, "template_jq 'peek' on template 'probe' takes params ['x'], got unknown ['y']"),
        ({"y": 1}, "template_jq 'peek' on template 'probe' takes params ['x'], missing ['x'], got unknown ['y']"),
    ],
)
async def test_an_input_program_called_with_a_missing_or_undeclared_param_is_refused_before_the_record_is_read(
    args: dict[str, Any], message: str
) -> None:
    record = _Record({"probe": {"n": 1}})
    with pytest.raises(ValueValidationError) as excinfo:
        await evaluate_input_program(_bound("$params", params=["x"]), args, record)
    assert str(excinfo.value) == message
    assert record.reads == 0


async def test_an_input_program_runs_over_the_attachment_subtree_with_the_member_variables_and_the_sibling_prelude():
    record = _Record({"probe": {"n": 1}, "other": {"n": 9}})
    program = _bound(
        "{dot: ., parameters: $parameters, declarations: $declarations, params: $params, sibling: tjq_twice({n: .n})}",
        params=["x"],
        prelude=template_jq_prelude({"twice": "$params.n * 2"}),
    )
    value = await evaluate_input_program(program, {"x": None}, record)
    assert value == {
        "dot": {"n": 1},
        "parameters": {"limit": 3},
        "declarations": {"allowed": ["a"]},
        "params": {"x": None},
        "sibling": 2,
    }
    assert record.reads == 1


@pytest.mark.parametrize(
    ("data", "path"),
    [
        (None, ["probe"]),
        (None, []),
        ({}, ["probe"]),
        ({"probe": 5}, ["probe"]),
        ({"probe": [1]}, ["probe"]),
        ({"probe": 5}, ["probe", "deeper"]),
    ],
)
async def test_a_missing_record_or_a_non_object_node_reads_as_an_empty_object(
    data: Mapping[str, Any] | None, path: list[str]
) -> None:
    assert await evaluate_input_program(_bound(".", path=path), {}, _Record(data)) == {}


async def test_a_sibling_call_resolves_within_the_programs_own_template_only() -> None:
    prelude = template_jq_prelude({"a": "1"})
    assert await evaluate_input_program(_bound("tjq_a({}) + 1", prelude=prelude), {}, _Record(None)) == 2
    for body in ("tjq_probe__a({})", "$states", "$attachments", "tjq_b({})"):
        with pytest.raises(ValueValidationError) as excinfo:
            await evaluate_input_program(_bound(body, prelude=prelude), {}, _Record(None))
        assert str(excinfo.value).startswith("template_jq 'peek' on template 'probe' failed to evaluate: ")
        assert "is not defined" in str(excinfo.value)


async def test_a_program_that_fails_to_evaluate_is_the_authors_refusal_naming_the_program() -> None:
    with pytest.raises(ValueValidationError) as excinfo:
        await evaluate_input_program(_bound('error("kaboom")'), {}, _Record(None))
    assert str(excinfo.value).startswith("template_jq 'peek' on template 'probe' failed to evaluate: ")
    assert "kaboom" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, JqProgramError)
    with pytest.raises(ValueValidationError, match=r"^template_jq 'peek' on template 'probe' failed to evaluate: "):
        await evaluate_input_program(_bound("this is (not valid"), {}, _Record(None))


async def test_a_program_that_emits_nothing_is_the_authors_refusal() -> None:
    with pytest.raises(ValueValidationError) as excinfo:
        await evaluate_input_program(_bound("empty"), {}, _Record(None))
    assert str(excinfo.value) == (
        "template_jq 'peek' on template 'probe' failed to evaluate: "
        "jq expression produced no output (empty pipeline): 'empty'"
    )


async def test_a_pool_fault_is_not_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _worker_lost(*_args: object, **_kwargs: object) -> object:
        raise ComputeWorkerLostError("the compute worker ended before it returned a result")

    monkeypatch.setattr(template_jq_mod, "run_jq_first", _worker_lost)
    with pytest.raises(ComputeWorkerLostError):
        await evaluate_input_program(_bound("."), {}, _Record(None))
    with pytest.raises(ComputeWorkerLostError):
        await derive_update_ops(_bound("[]"), None, _Record(None))


# --------------------------------------------------------------------------- #
# update programs                                                              #
# --------------------------------------------------------------------------- #
async def test_an_update_program_with_params_refuses_a_non_object_input_before_the_record_is_read() -> None:
    record = _Record({"probe": {"n": 1}})
    with pytest.raises(ValueValidationError) as excinfo:
        await derive_update_ops(replace(_bound("[]", params=["x"]), name="write"), 7, record)
    assert str(excinfo.value) == (
        "template_jq 'write' on template 'probe' declares params ['x'], so its input must be an object, got int"
    )
    assert record.reads == 0


@pytest.mark.parametrize(
    ("input_", "message"),
    [
        ({}, "template_jq 'write' on template 'probe' declares params ['x'], input missing ['x']"),
        ({"x": 1, "y": 2}, "template_jq 'write' on template 'probe' declares params ['x'], input has undeclared ['y']"),
        (
            {"y": 1},
            "template_jq 'write' on template 'probe' declares params ['x'], "
            "input missing ['x'], input has undeclared ['y']",
        ),
    ],
)
async def test_an_update_program_with_params_refuses_missing_or_undeclared_input_keys(
    input_: dict[str, Any], message: str
) -> None:
    record = _Record({"probe": {"n": 1}})
    with pytest.raises(ValueValidationError) as excinfo:
        await derive_update_ops(replace(_bound("[]", params=["x"]), name="write"), input_, record)
    assert str(excinfo.value) == message
    assert record.reads == 0


async def test_an_update_program_without_params_takes_any_input() -> None:
    body = '[{op: "set", path: ["n"], value: $input}]'
    for input_ in (7, "text", None, [1], {"any": "key"}):
        ops = await derive_update_ops(_bound(body), input_, _Record(None))
        assert ops == [{"op": "set", "path": ["probe", "n"], "value": input_}]


async def test_an_update_program_that_returns_no_list_is_refused() -> None:
    program = replace(_bound('{op: "set", path: ["n"], value: 1}'), name="write")
    with pytest.raises(ValueValidationError) as excinfo:
        await derive_update_ops(program, None, _Record(None))
    assert str(excinfo.value) == (
        "template_jq 'write' on template 'probe' is an update program, so its jq must return an op batch "
        "(a list of ops), got dict"
    )


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ('[{op: "set", path: "n", value: 1}]', "{'op': 'set', 'path': 'n', 'value': 1}"),
        ('[{op: "set", value: 1}]', "{'op': 'set', 'value': 1}"),
        ("[1]", "1"),
    ],
)
async def test_an_op_that_is_not_an_object_with_a_list_path_is_refused(body: str, shown: str) -> None:
    with pytest.raises(ValueValidationError) as excinfo:
        await derive_update_ops(_bound(body), None, _Record(None))
    assert str(excinfo.value) == f"an update-program op must be an object carrying a list path, got {shown}"


async def test_update_ops_are_rebased_under_the_attachment_path() -> None:
    body = (
        '[{op: "set", path: ["n"], value: (.n + $input.by)}, '
        '{op: "set", path: [], value: {limit: $parameters.limit, allowed: $declarations.allowed}, note: "kept"}]'
    )
    record = _Record({"outer": {"probe": {"n": 1}}})
    ops = await derive_update_ops(_bound(body, path=["outer", "probe"]), {"by": 2}, record)
    assert ops == [
        {"op": "set", "path": ["outer", "probe", "n"], "value": 3},
        {"op": "set", "path": ["outer", "probe"], "value": {"limit": 3, "allowed": ["a"]}, "note": "kept"},
    ]
    assert record.reads == 1


async def test_an_update_program_runs_with_its_templates_sibling_prelude_and_its_three_variables() -> None:
    prelude = template_jq_prelude({"seed": ".n + 10"})
    body = (
        '[{op: "set", path: ["n"], value: (tjq_seed({}) + $input.by)}, '
        '{op: "set", path: ["meta"], value: {limit: $parameters.limit, allowed: $declarations.allowed}}]'
    )
    record = _Record({"probe": {"n": 1}})
    ops = await derive_update_ops(_bound(body, prelude=prelude), {"by": 2}, record)
    assert ops == [
        {"op": "set", "path": ["probe", "n"], "value": 13},
        {"op": "set", "path": ["probe", "meta"], "value": {"limit": 3, "allowed": ["a"]}},
    ]


async def test_params_is_not_defined_in_an_update_program() -> None:
    program = replace(_bound('[{op: "set", path: ["n"], value: $params}]'), name="write")
    with pytest.raises(ValueValidationError) as excinfo:
        await derive_update_ops(program, {}, _Record(None))
    assert str(excinfo.value).startswith("template_jq 'write' on template 'probe' failed to evaluate: ")
    assert "$params is not defined" in str(excinfo.value)


async def test_an_update_program_that_fails_to_evaluate_is_the_authors_refusal_naming_the_program() -> None:
    program = replace(_bound('error("kaboom")'), name="write")
    with pytest.raises(ValueValidationError) as excinfo:
        await derive_update_ops(program, None, _Record(None))
    assert str(excinfo.value).startswith("template_jq 'write' on template 'probe' failed to evaluate: ")
    assert "kaboom" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, JqProgramError)
