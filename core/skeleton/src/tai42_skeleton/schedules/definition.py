"""The schedule definition check the create door and the schedules restore share: the door contract and the binding."""

from __future__ import annotations

from tai42_contract.interactions import ParkableDoorMixin
from tai42_contract.states import StateBinding

from tai42_skeleton.template.references import assert_templates_stored
from tai42_skeleton.template.stored_refs import door_contract_template_refs


async def check_schedule_definition(state_binding: StateBinding | None, contract: ParkableDoorMixin | None) -> None:
    """Check a schedule's door contract and binding; a schedule with neither passes.

    The stored template ids the contract's four expressions name must exist (a ``ValueError``
    with one line per missing id); then the binding is validated and its templates attached. A
    refusal raises the check's own error (a ``ValueError`` or a states refusal), which each
    caller classifies by type; any other error is a store or transport failure.
    """
    if contract is not None:
        await assert_templates_stored(door_contract_template_refs(contract), ValueError)
    if state_binding is None:
        return
    from tai42_skeleton.app import instance
    from tai42_skeleton.tools import state_binding as binding_validation

    await binding_validation.validate_and_attach_binding(instance.app, state_binding)
