"""The concrete validate + apply service composed from its concern mixins.

Holds the store and the consumer-owned registries; every method refuses loudly while the
feature is off. The feature gate reads ``states_store_configured`` THROUGH the
``states.service`` package so a test double bound at that alias drives it.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, ClassVar

from tai42_contract.states.errors import StatesNotConfiguredError

from tai42_skeleton.states.service.attachments import _AttachmentMixin
from tai42_skeleton.states.service.declarations import _DeclarationMixin
from tai42_skeleton.states.service.provenance import _ProvenanceMixin
from tai42_skeleton.states.service.reconcile import _ReconcileMixin
from tai42_skeleton.states.service.records import _RecordMixin
from tai42_skeleton.states.service.registration import _RegistrationMixin
from tai42_skeleton.states.service.registries import (
    StatesAttachReconcilerRegistry,
    StatesAttachValidatorRegistry,
    StatesConsumerListerRegistry,
)
from tai42_skeleton.states.service.template_jq import _TemplateJqMixin
from tai42_skeleton.states.service.templates import _TemplateMixin
from tai42_skeleton.states.store import PostgresStatesStore

if TYPE_CHECKING:
    from tai42_skeleton.states.seeds import StateTemplateSeedRegistry
    from tai42_skeleton.states.templates import StateTemplate


class StatesService(
    _ProvenanceMixin,
    _TemplateMixin,
    _TemplateJqMixin,
    _DeclarationMixin,
    _RecordMixin,
    _AttachmentMixin,
    _RegistrationMixin,
    _ReconcileMixin,
):
    """The one validate + apply layer. Holds a store and the consumer-owned registries;
    every method refuses loudly while the feature is off."""

    _TEMPLATE_CACHE_MAX: ClassVar[int] = 256

    def __init__(
        self,
        store: PostgresStatesStore | None = None,
        *,
        attach_validators: StatesAttachValidatorRegistry | None = None,
        attach_reconcilers: StatesAttachReconcilerRegistry | None = None,
        consumer_listers: StatesConsumerListerRegistry | None = None,
        seeds: StateTemplateSeedRegistry | None = None,
    ) -> None:
        from tai42_skeleton.states.seeds import StateTemplateSeedRegistry

        self._store = store or PostgresStatesStore()
        self._attach_validators = attach_validators or StatesAttachValidatorRegistry()
        self._attach_reconcilers = attach_reconcilers or StatesAttachReconcilerRegistry()
        self._consumer_listers = consumer_listers or StatesConsumerListerRegistry()
        self._seeds = seeds or StateTemplateSeedRegistry()
        self._template_cache: OrderedDict[tuple[str, Any], StateTemplate] = OrderedDict()
        # The platform's own template-document reconciler: it settles a state's open records
        # against a declarations edit through the template's ``reconcile`` contract. A no-op
        # for a first attach or a template that declares no ``reconcile``, so it is always on.
        self._attach_reconcilers.register(self._reconcile_template_records)

    @staticmethod
    def _ensure_available() -> None:
        from tai42_skeleton.states import service as _pkg

        if not _pkg.states_store_configured():
            raise StatesNotConfiguredError(
                "the states feature is off: bind the 'states' component's database (TAI_DB_BINDING_STATES / the "
                "default database) to enable it"
            )
