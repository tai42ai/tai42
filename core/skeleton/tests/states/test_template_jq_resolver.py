"""The one ``template_jq`` resolver: ``resolve_template_jq`` on the states facet.

A neutral synthetic state ``probe-state`` with templates ``alpha`` (attached at ``["a"]``) and
``beta`` (attached at ``["b"]``) — plus ``gamma``, stored but not attached — read through the real
:class:`StatesService` over the in-memory Postgres and served by the real :class:`StatesFacet`.
Every reference form (unqualified, qualified, ambiguous, wrong purpose, ``declared``) resolves to
the same verdict the evaluation doors (``eval_template_jq`` / ``apply_template_jq``) reach.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states import ResolvedTemplateJq
from tai42_contract.states.errors import StateNotFoundError, ValueValidationError
from tai42_contract.states.models import (
    AttachBody,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    WriteOrigin,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.app.facets.states import StatesFacet
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg

_SUBJECT = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")


def _template(name: str, programs: dict[str, dict[str, Any]], prop: str) -> dict[str, Any]:
    return {
        "kind": "state-template",
        "name": name,
        "schema": {"type": "object", "properties": {prop: {}}},
        "template_jq": programs,
    }


_ADD = {
    "purpose": "update",
    "params": ["v"],
    "writes": [["items"]],
    "jq": {"content": '[{"op": "set", "path": ["items"], "value": $input.v}]'},
}
_ALPHA = _template(
    "alpha",
    {
        "count": {"purpose": "input", "jq": {"content": "(.items // []) | length"}},
        "add": _ADD,
        "shared": {"purpose": "input", "jq": {"content": "1"}},
    },
    "items",
)
_BETA = _template(
    "beta",
    {
        "label": {"purpose": "input", "jq": {"content": '.label // ""'}},
        "shared": {"purpose": "input", "jq": {"content": "2"}},
    },
    "label",
)
_GAMMA = _template(
    "gamma",
    {"only": {"purpose": "input", "params": ["p"], "jq": {"content": "$params.p"}}, "add": _ADD},
    "items",
)


class _Manager:
    """The resource-manager surface the states service renders inline bodies through."""

    epoch = 1
    generation = 0
    cache_enabled = True

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        assert text.content is not None
        return text.content


@pytest.fixture
async def facet(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(SimpleNamespace(storage=SimpleNamespace(resource_manager=_Manager()))):  # type: ignore[arg-type]
        svc = StatesService(store=PostgresStatesStore())
        await svc.put_declaration(
            StateDeclaration(
                name="probe-state",
                schema={"type": "object", "properties": {"n": {}}},
                subject_kinds=["thread"],
                default_subject_kind="thread",
            )
        )
        for doc in (_ALPHA, _BETA, _GAMMA):
            await svc.put_template(StateTemplateDocument.model_validate(doc), replace=False)
        await svc.attach("probe-state", "alpha", AttachBody(path=["a"]))
        await svc.attach("probe-state", "beta", AttachBody(path=["b"]))
        yield StatesFacet(SimpleNamespace(_states_service=svc))  # type: ignore[arg-type]


async def test_an_unqualified_name_resolves_to_its_one_declaring_template(facet: StatesFacet) -> None:
    resolved = await facet.resolve_template_jq("probe-state", "count", purpose="input")
    assert resolved == ResolvedTemplateJq(template="alpha", program="count", purpose="input", params=[], path=["a"])


async def test_a_qualified_name_resolves_to_that_templates_program(facet: StatesFacet) -> None:
    resolved = await facet.resolve_template_jq("probe-state", "beta.shared", purpose="input")
    assert resolved == ResolvedTemplateJq(template="beta", program="shared", purpose="input", params=[], path=["b"])


async def test_the_resolution_carries_the_programs_params(facet: StatesFacet) -> None:
    resolved = await facet.resolve_template_jq("probe-state", "add", purpose="update")
    assert resolved.params == ["v"]
    assert resolved.purpose == "update"


async def test_an_ambiguous_unqualified_name_is_refused_naming_the_templates(facet: StatesFacet) -> None:
    with pytest.raises(ValueValidationError) as excinfo:
        await facet.resolve_template_jq("probe-state", "shared", purpose="input")
    assert str(excinfo.value) == (
        "template_jq 'shared' is declared by more than one template attached on state 'probe-state' "
        "(alpha, beta); qualify it as <template>.shared"
    )


async def test_a_wrong_purpose_is_refused_naming_both_purposes(facet: StatesFacet) -> None:
    with pytest.raises(ValueValidationError) as excinfo:
        await facet.resolve_template_jq("probe-state", "add", purpose="input")
    assert str(excinfo.value) == "template_jq 'add' on template 'alpha' has purpose 'update', needs 'input'"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("missing", "no template_jq 'missing' on any template attached on state 'probe-state'"),
        ("gamma.only", "template 'gamma' is not attached on state 'probe-state'"),
        ("alpha.missing", "template 'alpha' attached on state 'probe-state' declares no template_jq 'missing'"),
    ],
)
async def test_an_unknown_reference_is_not_found(facet: StatesFacet, name: str, message: str) -> None:
    with pytest.raises(StateNotFoundError) as excinfo:
        await facet.resolve_template_jq("probe-state", name, purpose="input")
    assert str(excinfo.value) == message


async def test_an_undeclared_state_is_not_found(facet: StatesFacet) -> None:
    with pytest.raises(StateNotFoundError, match="no state declared as 'ghost'"):
        await facet.resolve_template_jq("ghost", "count", purpose="input")


async def test_a_declared_template_not_attached_resolves_at_its_own_path(facet: StatesFacet) -> None:
    qualified = await facet.resolve_template_jq("probe-state", "gamma.only", purpose="input", declared=["gamma"])
    unqualified = await facet.resolve_template_jq("probe-state", "only", purpose="input", declared=["gamma"])
    expected = ResolvedTemplateJq(template="gamma", program="only", purpose="input", params=["p"], path=["gamma"])
    assert qualified == expected
    assert unqualified == expected


async def test_a_declared_template_joins_the_ambiguity_check(facet: StatesFacet) -> None:
    # ``add`` is unique among the declared templates (gamma) but alpha, attached, declares it too.
    with pytest.raises(ValueValidationError, match=r"\(alpha, gamma\); qualify it as <template>\.add"):
        await facet.resolve_template_jq("probe-state", "add", purpose="update", declared=["gamma"])


async def test_a_declared_template_already_attached_resolves_at_its_attachment(facet: StatesFacet) -> None:
    resolved = await facet.resolve_template_jq("probe-state", "label", purpose="input", declared=["beta"])
    assert resolved.path == ["b"]


async def test_a_declared_template_that_does_not_exist_is_not_found(facet: StatesFacet) -> None:
    with pytest.raises(StateNotFoundError, match="no template 'ghost'"):
        await facet.resolve_template_jq("probe-state", "only", purpose="input", declared=["ghost"])


async def test_the_evaluation_doors_refuse_a_wrong_purpose_with_the_resolver_message(facet: StatesFacet) -> None:
    with pytest.raises(ValueValidationError) as on_eval:
        await facet.eval_template_jq("probe-state", _SUBJECT, "add", {})
    assert str(on_eval.value) == "template_jq 'add' on template 'alpha' has purpose 'update', needs 'input'"
    with pytest.raises(ValueValidationError) as on_apply:
        await facet.apply_template_jq(
            "probe-state", _SUBJECT, "count", {}, op_id=None, origin=WriteOrigin(consumer="probe")
        )
    assert str(on_apply.value) == "template_jq 'count' on template 'alpha' has purpose 'input', needs 'update'"


async def test_the_evaluation_door_runs_the_program_the_resolver_names(facet: StatesFacet) -> None:
    await facet.apply_template_jq(
        "probe-state", _SUBJECT, "add", {"v": [1, 2]}, op_id=None, origin=WriteOrigin(consumer="probe")
    )
    resolved = await facet.resolve_template_jq("probe-state", "count", purpose="input")
    value = await facet.eval_template_jq("probe-state", _SUBJECT, f"{resolved.template}.{resolved.program}", {})
    assert value.value == 2
