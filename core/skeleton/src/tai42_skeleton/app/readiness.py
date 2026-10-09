"""Declared readiness targets: each subsystem names the backing stores ``/ready`` pings for it.

A subsystem registers one contributor under its own name on the app's
:class:`ReadinessRegistry`; the contributor returns the targets its current settings
wire (none when the subsystem is off), so ``/ready`` reads every subsystem through one
registry and never re-derives another module's gate. Each app owns its registry, so a
rebuilt app declares afresh; within one app a duplicate subsystem name is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from tai42_contract.access_control.identity import ReadinessTarget

ReadinessContributor = Callable[[], Sequence[ReadinessTarget]]


class ReadinessRegistry:
    """The readiness contributors one app declares, read in registration order."""

    def __init__(self) -> None:
        """Start with no contributor."""
        self._contributors: dict[str, ReadinessContributor] = {}

    def register(self, subsystem: str, contributor: ReadinessContributor) -> None:
        """Register ``subsystem``'s readiness contributor; a second one under the same name raises ``ValueError``."""
        if subsystem in self._contributors:
            raise ValueError(f"a readiness contributor is already registered for {subsystem!r}")
        self._contributors[subsystem] = contributor

    def wired_targets(self) -> list[ReadinessTarget]:
        """Every target the registered contributors wire now, in registration order."""
        return [target for contributor in self._contributors.values() for target in contributor()]


__all__ = ["ReadinessContributor", "ReadinessRegistry"]
