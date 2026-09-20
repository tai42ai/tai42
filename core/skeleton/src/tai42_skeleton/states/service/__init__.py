"""The one validate + apply layer over the subject-keyed record store — the platform half of the state feature.

Holds a :class:`~tai42_skeleton.states.store.PostgresStatesStore`; every door refuses
loudly (:class:`~tai42_contract.states.errors.StatesNotConfiguredError`, 501) while the
``states`` component's database is unbound. The service owns subject validation (the
``person`` kind against the identity store), the effective-schema composer, the template
lifecycle, and the WRITE-PROVENANCE CHOKEPOINT: it completes a consumer's
:class:`~tai42_contract.states.WriteOrigin` into a
:class:`~tai42_contract.states.CompletedOrigin` — stamping ``door``/``actor``/``turn_id``
from the ambient :class:`~tai42_contract.states.StateContext` (or ``api`` + the request
principal with none) — so the audit ledger is never optional or forgeable.

The feature gate reads ``states_store_configured`` and the retention sweep reads
``store_settings_default_retention`` THROUGH this package object at call time, so a test
double bound at the ``states.service`` alias drives them.
"""

from __future__ import annotations

from tai42_skeleton.states.context import current_state_context, state_context

# Re-exported at the package so the feature gate and the retention sweep — which read them
# THROUGH this package object at call time — and a test double bound at the ``states.service``
# alias drive the same name.
from tai42_skeleton.states.db import states_store_configured as states_store_configured
from tai42_skeleton.states.service.registries import (
    StatesAttachReconcilerRegistry,
    StatesAttachValidatorRegistry,
    StatesConsumerListerRegistry,
)
from tai42_skeleton.states.service.service import StatesService
from tai42_skeleton.states.store import store_settings_default_retention as store_settings_default_retention

__all__ = [
    "StatesAttachReconcilerRegistry",
    "StatesAttachValidatorRegistry",
    "StatesConsumerListerRegistry",
    "StatesService",
    "current_state_context",
    "state_context",
]
