"""The shared route door table both app-level middlewares match requests against.

A middleware that must answer "which registered route covers this request?" — the rate
limiter (charging the matched door's family budget) and the body-size cap (honouring the
matched door's declared bound) — compiles the registered surface into an ordered table of
matchers and consults it per request. The compile, the specificity ordering and the
registry-version memo are identical for both; only the per-route payload each cares about
differs. That common machinery lives here once.

A :class:`Door` carries the path matcher, the methods it answers (HEAD folded in wherever
GET is), the path template, the static-prefix length that orders the table most-specific
first, and a projected ``payload`` — whatever the consuming middleware needs about the
route. :func:`build_door_table` compiles a route iterable through a projection into that
ordered table; :class:`DoorTable` wraps it with the registry-version memo and the per-request
lookup.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from starlette.routing import compile_path

from tai42_skeleton.app.route_registry.metadata import RouteMetadata

__all__ = ["Door", "DoorTable", "build_door_table"]


@dataclass(frozen=True)
class Door[T]:
    """How a middleware recognises a request for one registered route.

    Carries the path matcher plus the ``payload`` a consuming middleware projected from the
    route's metadata.
    """

    pattern: re.Pattern[str]
    methods: frozenset[str]
    # The path TEMPLATE with every parameter as its ``{name}`` — the specificity tiebreak
    # and the only request text a refusal audit may record (a path-borne token never leaks).
    template: str
    # Characters before the first path parameter — the primary specificity ordering, so a
    # concrete route always outranks a more general one (the SPA catch-all) that also matches.
    static_prefix_length: int
    payload: T


def build_door_table[T](
    routes: Iterable[RouteMetadata], project: Callable[[RouteMetadata], T | None]
) -> tuple[Door[T], ...]:
    """Compile the routes ``project`` accepts into an ordered table of matchers, most specific first.

    ``project`` returns the payload a route contributes, or ``None`` to skip it (a body cap
    skips every route declaring no bound; the rate limiter projects them all). Ordering is by
    static prefix length then template length, both descending, so a concrete route always
    out-matches a more general one that also covers the path.
    """
    doors: list[Door[T]] = []
    for meta in routes:
        payload = project(meta)
        if payload is None:
            continue
        pattern, template, _ = compile_path(meta.path)
        methods = {method.upper() for method in meta.methods}
        if "GET" in methods:
            # Starlette answers HEAD from a GET route; a matcher must see the same.
            methods.add("HEAD")
        parameter = meta.path.find("{")
        doors.append(
            Door(
                pattern=pattern,
                methods=frozenset(methods),
                template=template,
                static_prefix_length=len(meta.path) if parameter == -1 else parameter,
                payload=payload,
            )
        )
    doors.sort(key=lambda door: (door.static_prefix_length, len(door.template)), reverse=True)
    return tuple(doors)


class DoorTable[T]:
    """A route door table memoized against the registry version that produced it.

    ``load_routes`` yields the registered surface and ``version_of`` reads the registry's
    monotonic version; both are read on demand so a test can swap the surface underneath a
    middleware. A reload re-records the surface and bumps the version, so the next lookup
    rebuilds instead of matching against the previous deployment's doors.
    """

    def __init__(
        self,
        project: Callable[[RouteMetadata], T | None],
        *,
        load_routes: Callable[[], Iterable[RouteMetadata]],
        version_of: Callable[[], int],
    ) -> None:
        """Build doors from ``load_routes`` through ``project``, memoized against ``version_of``."""
        self._project = project
        self._load_routes = load_routes
        self._version_of = version_of
        self._memo: tuple[int, tuple[Door[T], ...]] | None = None

    def door_for(self, method: str, path: str) -> Door[T] | None:
        """The most specific registered route covering this request, or ``None`` when none matches."""
        if self._memo is None or self._memo[0] != self._version_of():
            # Read the version BEFORE the build, so the memo can only ever UNDER-claim: a
            # route recorded while the table compiles (``load_routes`` may import router
            # modules; an epoch build records on its own thread) leaves the stored version
            # behind the registry's and the next request rebuilds. Reading it after would
            # stamp the table with a version whose doors it does not hold.
            version = self._version_of()
            self._memo = (version, build_door_table(self._load_routes(), self._project))
        for door in self._memo[1]:
            if method in door.methods and door.pattern.fullmatch(path):
                return door
        return None

    def reset(self) -> None:
        """Drop the memoized table so the next lookup recompiles it.

        Production invalidation rides the registry version; this is for a test that swaps the
        route surface underneath the middleware without recording into the live registry.
        """
        self._memo = None
