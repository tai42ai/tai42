"""The consumer-owned registries the service consults, reset each ``start()`` by the server.

A consumer registers an attach validator, an attach reconciler, or a consumer lister when
its module loads; the attach doors consult every registered entry before a write, and a
reload re-registers cleanly against a freshly reset registry.
"""

from __future__ import annotations

from tai42_contract.states.models import AttachReconciler, AttachValidator, ConsumerLister


class StatesAttachValidatorRegistry:
    """The process-wide attach-validator registry — the body behind ``app.states.register_attach_validator``.

    A consumer registers a data-dependent validator when its module loads; the attach doors consult every
    registered validator before any write. Reset each ``start()`` so a reload re-registers cleanly.
    """

    def __init__(self) -> None:
        """Start with an empty validator list."""
        self._validators: list[AttachValidator] = []

    def register(self, validator: AttachValidator) -> None:
        """Append ``validator`` to the registry."""
        self._validators.append(validator)

    def all(self) -> list[AttachValidator]:
        """A copy of the registered validators."""
        return list(self._validators)

    def reset(self) -> None:
        """Drop every registered validator."""
        self._validators.clear()


class StatesAttachReconcilerRegistry:
    """The process-wide attach-reconciler registry — the body behind ``app.states.register_attach_reconciler``.

    A consumer registers a pre-write reconciler when its module loads; the attach doors run every registered
    reconciler after the validators and before the write. Reset each ``start()`` so a reload re-registers
    cleanly.
    """

    def __init__(self) -> None:
        """Start with an empty reconciler list."""
        self._reconcilers: list[AttachReconciler] = []

    def register(self, reconciler: AttachReconciler) -> None:
        """Append ``reconciler`` to the registry."""
        self._reconcilers.append(reconciler)

    def all(self) -> list[AttachReconciler]:
        """A copy of the registered reconcilers."""
        return list(self._reconcilers)

    def reset(self) -> None:
        """Drop every registered reconciler."""
        self._reconcilers.clear()


class StatesConsumerListerRegistry:
    """The process-wide consumer-lister registry — the body behind ``app.states.register_consumer_lister``.

    A duplicate kind within one load raises loudly. Reset each ``start()`` so a reload re-registers cleanly.
    """

    def __init__(self) -> None:
        """Start with an empty ``{kind: lister}`` map."""
        self._listers: dict[str, ConsumerLister] = {}

    def register(self, kind: str, lister: ConsumerLister) -> None:
        """Register ``lister`` under ``kind``, raising loudly if the kind is already registered."""
        if kind in self._listers:
            raise ValueError(f"states consumer lister for kind {kind!r} is already registered")
        self._listers[kind] = lister

    def all(self) -> dict[str, ConsumerLister]:
        """A copy of the ``{kind: lister}`` map."""
        return dict(self._listers)

    def reset(self) -> None:
        """Drop every registered lister."""
        self._listers.clear()
