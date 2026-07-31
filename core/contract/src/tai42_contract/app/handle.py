"""``tai42_app`` — the runtime forwarding handle.

The one whitelisted behavioral member of the otherwise-pure contract: a minimal,
dependency-free stand-in that forwards attribute access to the concrete app impl,
injected once at startup via ``tai42_app.bind(impl)``. No third-party deps, no
business logic — only forwarding. Attribute access before ``bind`` raises loudly,
never a silent ``None``.

``bind`` is the one-shot startup injection and overwrites whatever it replaces; a
temporary binding must go through ``tai42_app.bound(impl)``, which restores the
previous one on exit.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


class _TaiAppHandle:
    """Forwards attribute access to the bound app impl; raises before ``bind``."""

    __slots__ = ("_impl",)

    def __init__(self) -> None:
        self._impl: Any = None

    def bind(self, impl: object) -> None:
        """Inject the concrete app impl. The runtime calls this once at startup."""
        self._impl = impl

    @contextmanager
    def bound(self, impl: object) -> Generator[None]:
        """Bind ``impl`` for the block, restoring exactly what was bound on entry
        (another impl, or the unbound state). Nesting is safe. Use this rather than
        ``bind(None)``, which erases a binding another component made.
        """
        previous = self._impl
        self._impl = impl
        try:
            yield
        finally:
            self._impl = previous

    def __getattr__(self, name: str) -> Any:
        # __getattr__ runs only when normal lookup misses (i.e. not _impl/bind/bound),
        # so every real app member routes here and forwards to the impl. Raising
        # AttributeError (not RuntimeError) keeps the attribute protocol intact:
        # hasattr/getattr(default)/inspect probes report absence loudly instead
        # of crashing, while the message still names the missing-bind cause.
        impl = object.__getattribute__(self, "_impl")
        if impl is None:
            raise AttributeError(
                f"tai42_app accessed before bind(): cannot resolve {name!r}. "
                "The runtime must call tai42_app.bind(app) at startup."
            )
        return getattr(impl, name)


tai42_app = _TaiAppHandle()

__all__ = ["tai42_app"]
