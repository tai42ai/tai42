"""The write-provenance chokepoint.

Completes a consumer's :class:`~tai42_contract.states.WriteOrigin` into a
:class:`~tai42_contract.states.CompletedOrigin`, stamping ``door``/``actor``/``turn_id``/
``inbound_id`` from the ambient :class:`~tai42_contract.states.StateContext` (or ``api`` +
the request principal with none), so the audit ledger is never optional or forgeable.
"""

from __future__ import annotations

from tai42_contract.states.models import CompletedOrigin, StateContext, WriteOrigin

from tai42_skeleton.states.context import current_state_context
from tai42_skeleton.states.service.base import _StatesServiceBase


class _ProvenanceMixin(_StatesServiceBase):
    def _complete_origin(self, origin: WriteOrigin) -> CompletedOrigin:
        """Complete a consumer's :class:`WriteOrigin` into a :class:`CompletedOrigin`:
        ``door``/``actor``/``turn_id``/``inbound_id`` from the ambient context, or ``api``
        + the request principal with none. The consumer's ``door``/``actor``/``turn_id``
        cannot be supplied (absent from :class:`WriteOrigin`, ``extra='forbid'``), so the
        ledger can never be forged."""
        ctx = current_state_context()
        if ctx is not None:
            return CompletedOrigin(
                consumer=origin.consumer,
                meta=origin.meta,
                run_id=origin.run_id,
                op_id=origin.op_id,
                door=ctx.door,
                actor=ctx.actor,
                turn_id=ctx.turn_id,
                inbound_id=ctx.inbound_id,
            )
        from tai42_skeleton.access_control.user import request_identity

        actor, _restricted = request_identity()
        return CompletedOrigin(
            consumer=origin.consumer,
            meta=origin.meta,
            run_id=origin.run_id,
            op_id=origin.op_id,
            door="api",
            actor=actor,
            turn_id=None,
            inbound_id=None,
        )

    def context(self) -> StateContext | None:
        return current_state_context()
