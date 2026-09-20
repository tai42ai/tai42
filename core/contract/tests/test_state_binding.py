"""The door-layer :class:`~tai42_contract.states.StateBinding` wire shape and its
carriage on every door definition."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.conversations import TargetConversationConfig
from tai42_contract.hooks import HookRegister
from tai42_contract.presets.models import PresetBody, PresetSeed
from tai42_contract.states import StateAttach, StateBinding, StateInjection, StateUpdate
from tai42_contract.template import TemplatedText
from tai42_contract.tools import ToolInvocation


def _binding() -> StateBinding:
    return StateBinding(
        states=[
            StateAttach(
                state="status",
                templates=["planner"],
                subject_expr=TemplatedText(content=".thread_id"),
                scope_expr=TemplatedText(content='{target_kind: "agent", target_name: .our_identity}'),
                input_injections=[
                    StateInjection(template_jq="bound", into="context"),
                    StateInjection(jq=TemplatedText(content="{record: .record, n: (.input.count)}"), into="extra"),
                ],
                updates=[
                    StateUpdate(
                        template_jq="mark",
                        adapter=TemplatedText(content="{verdict: .output.status}"),
                        op_id=TemplatedText(content=".input.turn"),
                    ),
                    StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["last"], value: .output}]')),
                ],
            )
        ]
    )


def test_state_binding_round_trips_both_injection_and_update_shapes() -> None:
    binding = _binding()
    again = StateBinding.model_validate(binding.model_dump())
    assert again == binding
    attach = again.states[0]
    assert attach.input_injections[0].template_jq == "bound"
    assert attach.input_injections[1].jq == TemplatedText(content="{record: .record, n: (.input.count)}")
    assert attach.input_injections[1].template_jq is None
    assert attach.updates[0].adapter == TemplatedText(content="{verdict: .output.status}")
    assert attach.updates[1].adapter is None


def test_a_jq_slot_carries_a_stored_id_source() -> None:
    binding = StateBinding(
        states=[
            StateAttach(
                state="status",
                subject_expr=TemplatedText(id="subject-program"),
                updates=[StateUpdate(jq=TemplatedText(id="batch-program"))],
            )
        ]
    )
    again = StateBinding.model_validate(binding.model_dump())
    assert again == binding
    assert again.states[0].subject_expr.id == "subject-program"
    assert again.states[0].updates[0].jq is not None
    assert again.states[0].updates[0].jq.id == "batch-program"


def test_injection_sets_exactly_one_source() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        StateInjection(template_jq="a", jq=TemplatedText(content="."), into="x")
    with pytest.raises(ValidationError, match="exactly one"):
        StateInjection(into="x")


def test_update_sets_exactly_one_source_and_custom_has_no_adapter() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        StateUpdate(template_jq="a", jq=TemplatedText(content="."))
    with pytest.raises(ValidationError, match="no 'adapter'"):
        StateUpdate(jq=TemplatedText(content="[]"), adapter=TemplatedText(content="{x: .output}"))


def test_attach_refuses_a_bad_state_name_and_requires_a_subject() -> None:
    with pytest.raises(ValidationError):
        StateAttach(state="Bad Name", subject_expr=TemplatedText(content=".id"))
    with pytest.raises(ValidationError):
        StateAttach.model_validate({"state": "status"})


def test_binding_requires_at_least_one_state() -> None:
    with pytest.raises(ValidationError):
        StateBinding(states=[])


def test_every_door_definition_carries_the_optional_binding() -> None:
    binding = _binding()
    assert PresetBody(base_tool="t", state_binding=binding).state_binding == binding
    assert PresetBody(base_tool="t").state_binding is None
    assert PresetSeed(name="p", description="d", base_tool="t", state_binding=binding).state_binding == binding
    assert TargetConversationConfig(target_kind="tool", target_name="n", state_binding=binding).state_binding == binding
    hook = HookRegister(name="h", topic="t", tool="x", execution_key="k", state_binding=binding)
    assert hook.state_binding == binding
    assert ToolInvocation(tool_name="x", state_binding=binding).state_binding == binding
    assert ToolInvocation(tool_name="x").state_binding is None
