"""The door-layer binding runtime: merge/precedence, subject resolution, inject-before,
update-after, and the save-time validate-and-attach seam — driven against a lightweight fake
``states`` facet (the real jq engine runs; only the store is faked)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.states import (
    ApplyResult,
    StateAttach,
    StateBinding,
    StateContext,
    StateDeclaration,
    StateInjection,
    StateRecord,
    StateSubject,
    StateTemplateDocument,
    StateUpdate,
    SubjectCandidates,
    TemplateJqApplyResult,
    TemplateJqResult,
    WriteOrigin,
)
from tai42_contract.states.errors import AttachConflictError, StateNotFoundError, ValueValidationError
from tai42_contract.template import TemplatedText

from tai42_skeleton.template.resource_manager import TemplateNotFoundError
from tai42_skeleton.tools.state_binding import (
    apply_binding_injections,
    apply_binding_updates,
    merge_bindings,
    validate_and_attach_binding,
    validate_binding,
)

_DECL = StateDeclaration(
    name="status", schema={"type": "object"}, subject_kinds=["thread"], default_subject_kind="thread"
)


class FakeStates:
    """The subset of the ``states`` facet the binding runtime calls."""

    def __init__(
        self,
        *,
        record: dict[str, Any] | None = None,
        attached: dict[str, list[str]] | None = None,
        templates: dict[str, StateTemplateDocument] | None = None,
        ctx: StateContext | None = None,
    ) -> None:
        self._record = record
        self._attached = attached or {}
        self._templates = templates or {}
        self._ctx = ctx
        self.eval_calls: list[tuple[str, StateSubject, str, dict[str, Any]]] = []
        self.apply_tjq_calls: list[tuple[str, StateSubject, str, Any, str | None, WriteOrigin]] = []
        self.apply_calls: list[tuple[str, StateSubject, list[dict[str, Any]], str | None, WriteOrigin]] = []
        self.attached_now: list[tuple[str, str, list[str]]] = []

    def context(self) -> StateContext | None:
        return self._ctx

    async def get_declaration(self, state: str) -> StateDeclaration | None:
        return _DECL if state == "status" else None

    async def read(self, state: str, subject: StateSubject) -> StateRecord | None:
        if self._record is None:
            return None
        return StateRecord(state=state, subject=subject, data=self._record, seq=1.0, canonical_subject=subject)

    async def eval_template_jq(
        self, state: str, subject: StateSubject, name: str, params: dict[str, Any]
    ) -> TemplateJqResult:
        self.eval_calls.append((state, subject, name, params))
        return TemplateJqResult(name=name, value={"evaluated": name})

    async def apply_template_jq(self, state, subject, name, input, *, op_id, origin) -> TemplateJqApplyResult:
        self.apply_tjq_calls.append((state, subject, name, input, op_id, origin))
        return TemplateJqApplyResult(name=name, applied=True, data={}, seq=1.0, skipped=[])

    async def apply(self, state, subject, ops, *, op_id, origin) -> ApplyResult:
        self.apply_calls.append((state, subject, ops, op_id, origin))
        return ApplyResult(applied=True, data={}, seq=1.0, skipped=[])

    async def list_attachments(self, state: str, *, template: str | None = None):
        names = self._attached.get(state, [])
        rows = [{"state": state, "template": t} for t in names]
        if template is not None:
            return [r for r in rows if r["template"] == template]
        return rows

    async def attach(self, state: str, template: str, body) -> None:
        if template in self._attached.get(state, []):
            raise AttachConflictError(f"{template} already attached on {state}")
        self.attached_now.append((state, template, list(body.path)))
        self._attached.setdefault(state, []).append(template)

    async def get_template(self, name: str) -> StateTemplateDocument | None:
        return self._templates.get(name)


class FakeResourceManager:
    """The subset of the resource manager the binding render seam calls. Renders a slot's
    templated text to its jq program: an inline ``content`` is its own text; a stored ``id``
    is looked up in ``resources`` and a MISSING id raises loudly (the store's own not-found
    error), so the door's by-id save check fails just as it would against a real store."""

    def __init__(self, resources: dict[str, str] | None = None) -> None:
        self._resources = resources or {}

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        if text.id is not None:
            if text.id not in self._resources:
                raise TemplateNotFoundError(f"template {text.id!r} not found")
            return self._resources[text.id]
        assert text.content is not None
        return text.content


def _app(states: FakeStates, resources: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(states=states, storage=SimpleNamespace(resource_manager=FakeResourceManager(resources)))


# -- merge / precedence -------------------------------------------------------
def test_merge_door_wins_subject_unions_templates_concats_and_appends_preset_only() -> None:
    door = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".a"),
                templates=["t1"],
                input_injections=[StateInjection(jq=TemplatedText(content=".record"), into="d")],
            )
        ]
    )
    preset = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".b"),
                templates=["t2"],
                input_injections=[StateInjection(jq=TemplatedText(content=".record"), into="p")],
            ),
            StateAttach(state="events", subject_expr=TemplatedText(content=".c")),
        ]
    )
    merged = merge_bindings(door, preset)
    assert merged is not None
    assert [a.state for a in merged.states] == ["status", "events"]
    status = merged.states[0]
    assert status.subject_expr == TemplatedText(content=".a")  # door wins
    assert status.templates == ["t1", "t2"]  # union, door-first
    assert [i.into for i in status.input_injections] == ["d", "p"]  # concat door-first
    assert merge_bindings(None, preset) is preset
    assert merge_bindings(door, None) is door


def test_merge_scope_expr_door_wins_when_present_else_preset() -> None:
    # M4: the door's scope_expr wins WHEN PRESENT; when the door names none, the preset's is used.
    door_with = StateBinding(
        states=[
            StateAttach(
                state="status", subject_expr=TemplatedText(content=".a"), scope_expr=TemplatedText(content=".d")
            )
        ]
    )
    door_without = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(content=".a"))])
    preset = StateBinding(
        states=[
            StateAttach(
                state="status", subject_expr=TemplatedText(content=".b"), scope_expr=TemplatedText(content=".p")
            )
        ]
    )
    won = merge_bindings(door_with, preset)
    assert won is not None
    assert won.states[0].scope_expr == TemplatedText(content=".d")  # door's wins when present
    fell_back = merge_bindings(door_without, preset)
    assert fell_back is not None
    assert fell_back.states[0].scope_expr == TemplatedText(content=".p")  # preset's used when door names none


# -- subject resolution -------------------------------------------------------
async def test_subject_from_full_object_expression() -> None:
    states = FakeStates(record={"seen": True})
    # subject_expr yields a FULL subject object → used directly, no ambient scope needed.
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(
                    content='{target_kind: "agent", target_name: "a", kind: "thread", key: .id}'
                ),
                input_injections=[StateInjection(jq=TemplatedText(content=".record.seen"), into="out")],
            )
        ]
    )
    args = {"id": "t-1"}
    await apply_binding_injections(_app(states), b, args)
    assert args["out"] is True


async def test_scope_expr_false_skips_the_state_for_the_run() -> None:
    ctx = StateContext(door="conversation", candidates=SubjectCandidates(target_kind="agent", target_name="a"))
    states = FakeStates(record={"n": 5}, ctx=ctx)
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".tid"),
                scope_expr=TemplatedText(content=".enabled"),  # a boolean predicate over the run input
                input_injections=[StateInjection(jq=TemplatedText(content=".record.n"), into="n")],
            )
        ]
    )
    args = {"tid": "t-9", "enabled": False}
    await apply_binding_injections(_app(states), b, args)
    assert "n" not in args  # the false predicate skipped the whole state
    assert states.eval_calls == []


async def test_scope_expr_true_engages_the_state() -> None:
    ctx = StateContext(door="conversation", candidates=SubjectCandidates(target_kind="agent", target_name="a"))
    states = FakeStates(record={"n": 5}, ctx=ctx)
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".tid"),
                scope_expr=TemplatedText(content=".enabled"),
                input_injections=[StateInjection(jq=TemplatedText(content=".record.n"), into="n")],
            )
        ]
    )
    args = {"tid": "t-9", "enabled": True}
    await apply_binding_injections(_app(states), b, args)
    assert args["n"] == 5


async def test_scope_expr_non_boolean_is_loud() -> None:
    ctx = StateContext(door="conversation", candidates=SubjectCandidates(target_kind="agent", target_name="a"))
    states = FakeStates(record={}, ctx=ctx)
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".tid"),
                scope_expr=TemplatedText(content='"nope"'),
                input_injections=[StateInjection(jq=TemplatedText(content="."), into="x")],
            )
        ]
    )
    with pytest.raises(ValueValidationError, match="must yield a boolean"):
        await apply_binding_injections(_app(states), b, {"tid": "k"})


async def test_subject_key_with_no_scope_and_no_ambient_context_is_loud() -> None:
    states = FakeStates(ctx=None)
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".tid"),
                input_injections=[StateInjection(jq=TemplatedText(content="."), into="x")],
            )
        ]
    )
    with pytest.raises(ValueValidationError, match="no ambient subject scope"):
        await apply_binding_injections(_app(states), b, {"tid": "k"})


async def test_subject_uses_ambient_context_scope_and_default_kind() -> None:
    ctx = StateContext(door="conversation", candidates=SubjectCandidates(target_kind="agent", target_name="a"))
    states = FakeStates(record={}, ctx=ctx)
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".tid"),
                input_injections=[StateInjection(template_jq="bound", into="c")],
            )
        ]
    )
    args = {"tid": "k-1"}
    await apply_binding_injections(_app(states), b, args)
    # eval_template_jq was called with the resolved subject (thread/default kind + ambient scope)
    _state, subject, name, params = states.eval_calls[0]
    assert subject.kind == "thread"
    assert subject.key == "k-1"
    assert subject.target_name == "a"
    assert name == "bound"
    assert params == {}
    assert args["c"] == {"evaluated": "bound"}


# -- injections ---------------------------------------------------------------
async def test_named_injection_calls_eval_and_custom_runs_jq_over_record_and_input() -> None:
    ctx = StateContext(door="conversation", candidates=SubjectCandidates(target_kind="agent", target_name="a"))
    states = FakeStates(record={"count": 3}, ctx=ctx)
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".tid"),
                input_injections=[
                    StateInjection(template_jq="view", into="v"),
                    StateInjection(jq=TemplatedText(content="{c: .record.count, given: .input.tid}"), into="w"),
                ],
            )
        ]
    )
    args = {"tid": "k"}
    await apply_binding_injections(_app(states), b, args)
    assert args["v"] == {"evaluated": "view"}
    assert args["w"] == {"c": 3, "given": "k"}


# -- updates ------------------------------------------------------------------
async def test_named_update_adapts_input_and_custom_update_authors_ops() -> None:
    ctx = StateContext(door="schedule", candidates=SubjectCandidates(target_kind="agent", target_name="a"))
    states = FakeStates(record={"last": None}, ctx=ctx)
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".tid"),
                updates=[
                    StateUpdate(
                        template_jq="mark",
                        adapter=TemplatedText(content="{verdict: .output.status}"),
                        op_id=TemplatedText(content=".input.tid"),
                    ),
                    StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["last"], value: .output}]')),
                ],
            )
        ]
    )
    args = {"tid": "k-7"}
    output = {"status": "done"}
    await apply_binding_updates(_app(states), b, args, output, door_id="preset-x")
    # named update: adapter shaped the input, op_id resolved, template-independent consumer label
    _s, _subj, name, adapted, op_id, origin = states.apply_tjq_calls[0]
    assert name == "mark"
    assert adapted == {"verdict": "done"}
    assert op_id == "k-7"
    assert origin.consumer == "template_jq"
    assert origin.meta == {"template_jq": "mark"}
    # custom update: authored the batch, door-scoped writer
    _s2, _subj2, ops, _op2, origin2 = states.apply_calls[0]
    assert ops == [{"op": "set", "path": ["last"], "value": {"status": "done"}}]
    assert origin2.consumer == "door:preset-x"


# -- attach-on-use / validate at save -----------------------------------------
async def test_attach_on_use_attaches_absent_and_skips_present_idempotently() -> None:
    tpl = StateTemplateDocument.model_validate(
        {"name": "planner", "schema": {"type": "object"}, "template_jq": {"v": {"purpose": "input", "jq": "."}}}
    )
    states = FakeStates(attached={"status": ["planner"]}, templates={"planner": tpl})
    # 'planner' already attached → skipped; 'other' absent → attached once.
    tpl2 = StateTemplateDocument.model_validate({"name": "other", "schema": {"type": "object"}, "template_jq": {}})
    states._templates["other"] = tpl2
    b = StateBinding(
        states=[StateAttach(state="status", subject_expr=TemplatedText(content=".id"), templates=["planner", "other"])]
    )
    await validate_and_attach_binding(_app(states), b)
    assert states.attached_now == [("status", "other", ["other"])]


async def test_attach_failure_or_occupied_path_fails_the_save() -> None:
    # attach raising (an occupied path / conflict) propagates loudly.
    class BoomStates(FakeStates):
        async def attach(self, state, template, body):
            raise AttachConflictError("occupied")

    b = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(content=".id"), templates=["dup"])])
    with pytest.raises(AttachConflictError):
        await validate_and_attach_binding(_app(BoomStates(attached={"status": []})), b)


async def test_named_update_without_adapter_but_declared_params_is_refused_at_save() -> None:
    tpl = StateTemplateDocument.model_validate(
        {
            "name": "planner",
            "schema": {"type": "object"},
            "template_jq": {"put": {"purpose": "update", "params": ["id"], "writes": [], "jq": "[]"}},
        }
    )
    states = FakeStates(attached={"status": ["planner"]}, templates={"planner": tpl})
    b = StateBinding(
        states=[
            StateAttach(
                state="status", subject_expr=TemplatedText(content=".id"), updates=[StateUpdate(template_jq="put")]
            )
        ]
    )
    with pytest.raises(ValueValidationError, match="no adapter"):
        await validate_and_attach_binding(_app(states), b)


async def test_validate_binding_resolves_a_declared_template_without_attaching() -> None:
    # The dry-run (validate) seam performs NO attach: a named program in a template the
    # binding declares to attach resolves against the declared set, and nothing is attached.
    tpl = StateTemplateDocument.model_validate(
        {"name": "planner", "schema": {"type": "object"}, "template_jq": {"v": {"purpose": "input", "jq": "."}}}
    )
    states = FakeStates(attached={"status": []}, templates={"planner": tpl})
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".id"),
                templates=["planner"],
                input_injections=[StateInjection(template_jq="v", into="x")],
            )
        ]
    )
    await validate_binding(_app(states), b)
    assert states.attached_now == []  # dry run attaches nothing


async def test_validate_binding_compiles_scope_custom_and_qualified_named_exprs() -> None:
    # One binding exercising every validate branch WITHOUT attaching: scope_expr, a custom-jq
    # injection, a custom-jq update with an op_id, and a qualified named update carrying an
    # adapter — the named one resolved against a declared (un-attached) template while an
    # unrelated attached template is skipped.
    planner = StateTemplateDocument.model_validate(
        {"name": "planner", "schema": {"type": "object"}, "template_jq": {"mark": {"purpose": "update", "jq": "."}}}
    )
    states = FakeStates(attached={"status": ["other"]}, templates={"planner": planner})
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".id"),
                scope_expr=TemplatedText(content=".ok"),
                templates=["planner"],
                input_injections=[StateInjection(jq=TemplatedText(content="{v: .record}"), into="x")],
                updates=[
                    StateUpdate(jq=TemplatedText(content="[]"), op_id=TemplatedText(content=".output.id")),
                    StateUpdate(template_jq="planner.mark", adapter=TemplatedText(content="{v: 1}")),
                ],
            )
        ]
    )
    await validate_binding(_app(states), b)
    assert states.attached_now == []


async def test_validate_binding_qualified_unknown_template_is_refused() -> None:
    states = FakeStates(attached={"status": []}, templates={})
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".id"),
                updates=[StateUpdate(template_jq="ghost.mark")],
            )
        ]
    )
    with pytest.raises(StateNotFoundError, match="not attached"):
        await validate_binding(_app(states), b)


async def test_validate_binding_ambiguous_program_is_refused() -> None:
    tpl_a = StateTemplateDocument.model_validate(
        {"name": "a", "schema": {"type": "object"}, "template_jq": {"mark": {"purpose": "update", "jq": "."}}}
    )
    tpl_b = StateTemplateDocument.model_validate(
        {"name": "b", "schema": {"type": "object"}, "template_jq": {"mark": {"purpose": "update", "jq": "."}}}
    )
    states = FakeStates(attached={"status": ["a", "b"]}, templates={"a": tpl_a, "b": tpl_b})
    b = StateBinding(
        states=[
            StateAttach(
                state="status", subject_expr=TemplatedText(content=".id"), updates=[StateUpdate(template_jq="mark")]
            )
        ]
    )
    with pytest.raises(ValueValidationError, match="more than one"):
        await validate_binding(_app(states), b)


async def test_validate_binding_purpose_mismatch_is_refused() -> None:
    tpl = StateTemplateDocument.model_validate(
        {"name": "planner", "schema": {"type": "object"}, "template_jq": {"vin": {"purpose": "input", "jq": "."}}}
    )
    states = FakeStates(attached={"status": ["planner"]}, templates={"planner": tpl})
    b = StateBinding(
        states=[
            StateAttach(
                state="status", subject_expr=TemplatedText(content=".id"), updates=[StateUpdate(template_jq="vin")]
            )
        ]
    )
    with pytest.raises(ValueValidationError, match="has purpose"):
        await validate_binding(_app(states), b)


async def test_validate_binding_missing_declared_template_is_a_loud_refusal() -> None:
    # A declared template that does not exist cannot be attached — the dry run refuses it,
    # the SAME rejection the save seam's attach would raise, without attaching.
    states = FakeStates(attached={"status": []}, templates={})
    b = StateBinding(
        states=[StateAttach(state="status", subject_expr=TemplatedText(content=".id"), templates=["ghost"])]
    )
    with pytest.raises(StateNotFoundError, match="does not exist"):
        await validate_binding(_app(states), b)
    assert states.attached_now == []


async def test_named_injection_referencing_unknown_program_is_refused_at_save() -> None:
    tpl = StateTemplateDocument.model_validate({"name": "planner", "schema": {"type": "object"}, "template_jq": {}})
    states = FakeStates(attached={"status": ["planner"]}, templates={"planner": tpl})
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(content=".id"),
                input_injections=[StateInjection(template_jq="ghost", into="x")],
            )
        ]
    )
    with pytest.raises(StateNotFoundError):
        await validate_and_attach_binding(_app(states), b)


# -- by-id slots: render-then-compile at save --------------------------------
async def test_by_id_slot_that_cannot_be_fetched_fails_the_save_loudly() -> None:
    # A slot supplied by ``id`` is fetched and rendered at save before its jq is compiled; a
    # referenced resource that does not exist is a LOUD refusal naming the slot and the id,
    # never a silent pass or a deferred surprise.
    states = FakeStates(attached={"status": []}, templates={})
    b = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(id="ghost-expr"))])
    with pytest.raises(ValueValidationError, match=r"subject_expr.*'ghost-expr'"):
        await validate_and_attach_binding(_app(states, resources={}), b)


async def test_by_id_slots_resolve_and_compile_at_save() -> None:
    # By-id slots across every jq position resolve to their programs, which then compile with
    # the binding's prelude; the save accepts them and attaches nothing beyond the declared use.
    states = FakeStates(attached={"status": []}, templates={})
    resources = {
        "subj": ".id",
        "scope": ".ok",
        "inj": "{v: .record}",
        "upd": "[]",
        "op": ".output.id",
    }
    b = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(id="subj"),
                scope_expr=TemplatedText(id="scope"),
                input_injections=[StateInjection(jq=TemplatedText(id="inj"), into="x")],
                updates=[StateUpdate(jq=TemplatedText(id="upd"), op_id=TemplatedText(id="op"))],
            )
        ]
    )
    await validate_and_attach_binding(_app(states, resources=resources), b)
    assert states.attached_now == []
