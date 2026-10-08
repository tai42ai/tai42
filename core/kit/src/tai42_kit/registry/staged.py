"""Staged generations: a registry built off to the side and promoted in one reference assignment.

A process-global registry that a rebuild re-populates while the live one keeps serving holds two
generations: the COMMITTED one every reader resolves against, and a STAGED one a rebuild writes into.
The rebuild promotes the staged generation in one reference assignment (atomic under the GIL) when it
succeeds and drops it when it fails, so a reader never observes a torn rebuild. These primitives know
nothing of who drives the staging; each registry keeps its own registration rules on top of them.
Single-writer: the caller serialises rebuilds, and nothing on the serving path writes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class StagedGeneration[C]:
    """A committed container plus an optional staged replacement built by ``empty()``."""

    def __init__(self, empty: Callable[[], C]) -> None:
        """Start with an empty committed generation and no staged one."""
        self._empty = empty
        self._committed: C = empty()
        self._staged: C | None = None

    @property
    def staging(self) -> bool:
        """Whether a staged generation is open."""
        return self._staged is not None

    def begin(self) -> None:
        """Open a fresh, empty staged generation; the committed one keeps serving untouched."""
        self._staged = self._empty()

    def commit(self) -> None:
        """Promote the staged generation in one reference assignment; a no-op when none is open."""
        if self._staged is not None:
            self._committed = self._staged
            self._staged = None

    def abort(self) -> None:
        """Drop the staged generation; the committed one never saw it. A no-op when none is open."""
        self._staged = None

    def write_target(self) -> C:
        """Where a registration lands: the staged generation while staging, else the committed one."""
        return self._staged if self._staged is not None else self._committed

    def committed(self) -> C:
        """The committed generation — the serving read."""
        return self._committed


class StagedSlot[T]:
    """One live value plus an optional staged replacement.

    ``on_replace`` receives the displaced live value whenever a new value replaces it (a direct
    ``set`` outside staging, or a ``commit`` that promotes a staged value), so the owner can release
    what the old value holds.
    """

    def __init__(self, on_replace: Callable[[T], None] | None = None) -> None:
        """Start empty and not staging."""
        self._on_replace = on_replace
        self._current: T | None = None
        self._staged: T | None = None
        self._staging = False

    def _replace(self, value: T) -> None:
        old = self._current
        self._current = value
        if old is not None and self._on_replace is not None:
            self._on_replace(old)

    def begin(self) -> None:
        """Open staging with nothing staged: a ``set`` now stages instead of replacing the live value."""
        self._staging = True
        self._staged = None

    def set(self, value: T) -> None:
        """Stage ``value`` while staging, else replace the live value at once."""
        if self._staging:
            self._staged = value
            return
        self._replace(value)

    def commit(self) -> None:
        """Promote the staged value if one was set, else keep the live value; staging ends."""
        staged = self._staged
        self._staged = None
        if self._staging and staged is not None:
            self._replace(staged)
        self._staging = False

    def abort(self) -> None:
        """Drop the staged value and end staging; the live value is untouched."""
        self._staged = None
        self._staging = False

    def reset(self) -> None:
        """Empty the slot and end staging without calling ``on_replace``."""
        self._current = None
        self._staged = None
        self._staging = False

    def current(self) -> T | None:
        """The live value — the serving read."""
        return self._current

    def staged_or_current(self) -> T | None:
        """The staged value if one was set, else the live value — a rebuild's own read."""
        return self._staged if self._staged is not None else self._current


def same_factory(existing: object, factory: object) -> bool:
    """Whether two registered factories denote the SAME provider — the reload-safety predicate.

    True when the factories are the same object, OR when they share ``__module__`` and
    ``__qualname__``: the hot-reload primitive pops a plugin's modules and re-executes their bodies,
    minting a FRESH class object each pass, so a re-registration after a reload carries a new object
    that is nonetheless the same declared provider. A genuinely different provider has a different
    qualified name, so a real name collision still reads as different. Lambdas (qualname
    ``<lambda>``) never match across distinct objects — a factory with no stable qualified identity is
    treated as different, never silently coalesced.
    """
    if existing is factory:
        return True
    existing_qualname = getattr(existing, "__qualname__", None)
    if existing_qualname is None or "<lambda>" in existing_qualname:
        return False
    same_module = getattr(existing, "__module__", None) == getattr(factory, "__module__", None)
    return same_module and existing_qualname == getattr(factory, "__qualname__", None)


class NamedFactoryRegistry[F]:
    """Name → factory registrations over a :class:`StagedGeneration`, reload-safe by :func:`same_factory`.

    ``kind`` names the registered thing in log lines and errors (``"Identity provider"``).
    """

    def __init__(self, kind: str) -> None:
        """Create an empty registry of ``kind``."""
        self._kind = kind
        self._generation: StagedGeneration[dict[str, F]] = StagedGeneration(dict)

    def register(self, name: str, factory: F, *, before_add: Callable[[], None] | None = None) -> None:
        """Register ``factory`` under ``name`` in the write target.

        Re-registering the SAME factory (by :func:`same_factory`) under a name it already holds is a
        quiet no-op, so a reload re-executing a plugin's module body does not raise; a DIFFERENT factory
        under a held name raises ``ValueError``. ``before_add`` runs only for a new name, after those
        checks and before the factory lands, so a raise from it leaves this registry untouched.
        """
        target = self._generation.write_target()
        existing = target.get(name)
        if existing is not None:
            if same_factory(existing, factory):
                logger.debug("%s %s re-registered (reload no-op)", self._kind, name)
                return
            raise ValueError(f"{self._kind} {name!r} already registered")
        if before_add is not None:
            before_add()
        target[name] = factory
        logger.info("registered %s %s", self._kind.lower(), name)

    def get(self, name: str) -> F:
        """The factory under ``name`` in the COMMITTED generation; ``KeyError`` when absent."""
        return self._lookup(self._generation.committed(), name)

    def get_staged(self, name: str) -> F:
        """The factory under ``name`` in the staged generation while staging, else the committed one."""
        return self._lookup(self._generation.write_target(), name)

    def _lookup(self, generation: dict[str, F], name: str) -> F:
        factory = generation.get(name)
        if factory is None:
            raise KeyError(f"Unknown {self._kind.lower()}: {name!r}")
        return factory

    def names_staged(self) -> list[str]:
        """Name-sorted names in the staged generation while staging, else the committed one."""
        return sorted(self._generation.write_target())

    def items(self) -> list[tuple[str, F]]:
        """A fresh name-sorted snapshot of the COMMITTED registrations."""
        return sorted(self._generation.committed().items())

    def items_staged(self) -> list[tuple[str, F]]:
        """A fresh name-sorted snapshot of the staged generation while staging, else the committed one."""
        return sorted(self._generation.write_target().items())

    def reset(self) -> None:
        """Clear the write target: the staged generation while staging, else the committed one."""
        self._generation.write_target().clear()

    def begin_staging(self) -> None:
        """Open a fresh staged generation; the committed one keeps serving."""
        self._generation.begin()

    def commit_staging(self) -> None:
        """Promote the staged generation in one reference assignment; a no-op when none is open."""
        self._generation.commit()

    def abort_staging(self) -> None:
        """Drop the staged generation; a no-op when none is open."""
        self._generation.abort()
