"""Which plugin layer pins this image's ``tai42-contract`` rejects.

A pin whose declared contract range the running contract is outside — the state a core
upgrade leaves an install-store row in — is left out of the plugin layer, with the compat
verdict's reason (:func:`~tai42_skeleton.marketplace.compat.declared_compat`); the layer is
built from the rest. Pure: no I/O, no pip.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from tai42_skeleton.marketplace.compat import declared_compat
from tai42_skeleton.marketplace.environments.layout import Excluded
from tai42_skeleton.marketplace.environments.targets import PluginPin


@dataclass(frozen=True)
class Partition:
    """The pins the layer installs, and the ones it leaves out."""

    included: tuple[PluginPin, ...]
    excluded: tuple[Excluded, ...]


def partition(pins: Iterable[PluginPin]) -> Partition:
    """Split ``pins`` into those the running contract supports and those it rejects, with the reason."""
    included: list[PluginPin] = []
    excluded: list[Excluded] = []
    for pin in pins:
        verdict = declared_compat(pin.input.package, pin.contract)
        if verdict.status == "incompatible":
            assert verdict.reason is not None  # noqa: S101 (an incompatible CompatVerdict always carries its reason)
            excluded.append(Excluded(input=pin.input, modules=pin.modules, reason=verdict.reason))
        else:
            included.append(pin)
    return Partition(included=tuple(included), excluded=tuple(excluded))
