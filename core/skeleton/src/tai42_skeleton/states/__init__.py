"""The platform subject-keyed state store — the record substrate a door-agnostic
contract facet (``tai42_app.states``) reads and writes a subject's document through.

This package owns the Postgres seam (:mod:`.store`), the validate + apply service
(:mod:`.service`) with the write-provenance chokepoint, the platform module document
model (:mod:`.modules`), the pure op/path engine (:mod:`.paths`), the component
identity and boot gate (:mod:`.db`), the shipped-module seed applier (:mod:`.seeds`),
and the backup section (:mod:`.backup`). The doors, routers and builtin tools live in
their neighbouring feature packages, reaching the store through the service and facet.
"""

from __future__ import annotations

from tai42_skeleton.states.db import STATES_COMPONENT, states_store_configured
from tai42_skeleton.states.service import (
    StatesConsumerListerRegistry,
    StatesMountValidatorRegistry,
    StatesService,
    current_state_context,
    state_context,
)
from tai42_skeleton.states.store import PostgresStatesStore

__all__ = [
    "STATES_COMPONENT",
    "PostgresStatesStore",
    "StatesConsumerListerRegistry",
    "StatesMountValidatorRegistry",
    "StatesService",
    "current_state_context",
    "state_context",
    "states_store_configured",
]
