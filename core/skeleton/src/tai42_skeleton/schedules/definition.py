"""The schedule definition check the create door and the schedules restore share."""

from __future__ import annotations

from tai42_contract.states import StateBinding


async def check_schedule_definition(state_binding: StateBinding | None) -> None:
    """Validate a schedule's door binding and attach its templates; a schedule without one passes.

    A refusal raises the binding validation's own error (a ``ValueError`` or a states refusal),
    which each caller classifies by type; any other error is a store or transport failure.
    """
    if state_binding is None:
        return
    from tai42_skeleton.app import instance
    from tai42_skeleton.tools import state_binding as binding_validation

    await binding_validation.validate_and_attach_binding(instance.app, state_binding)
