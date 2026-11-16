"""The bound-route gate of the preset version writes.

A save or a rollback is refused with a 409 when the version it would make active leaves a
conversation route bound to the preset, or to a preset that composes it, unable to bind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tai42_skeleton.conversations.target_validators import active_target_candidate_body, target_bind_refusal_lines
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations.reference_graph import composing_closure, routes_bound_to
from tai42_skeleton.presets.candidate_scope import candidate_bodies

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SaveWrite:
    """A save: the body judged is the new version the save appends."""

    def refusal_opening(self, name: str) -> str:
        """The refusal's opening: the write refused, then the version that would break the routes."""
        return f"preset {name!r} cannot be saved — the new version"

    @property
    def judged(self) -> str:
        """How a refusal line names the version judged."""
        return "the new version"


@dataclass(frozen=True)
class RollbackWrite:
    """A rollback: the body judged is the existing ``version`` the active pointer moves to."""

    version: int

    def refusal_opening(self, name: str) -> str:
        """The refusal's opening: the write refused, then the version that would break the routes."""
        return f"preset {name!r} cannot be rolled back to version {self.version} — that version"

    @property
    def judged(self) -> str:
        """How a refusal line names the version judged."""
        return f"version {self.version}"


PresetWrite = SaveWrite | RollbackWrite


async def assert_bound_routes_still_bind(name: str, candidate_body: Any, write: PresetWrite) -> None:
    """Refuse a preset write whose version would break a conversation route bound to it or a composer.

    Re-runs the route bind check of every route bound to ``name`` AND every route bound to a preset that
    transitively composes ``name`` (:func:`composing_closure`), against ``candidate_body`` — the version
    ``write`` would make active, before it does: a write that would leave a bound route unable to bind
    is a 409 that commits nothing, naming the write, each route and the preset it is bound to.

    For a route bound directly to ``name`` the candidate IS ``candidate_body``, and every refusal line
    counts. For a route bound to a composer the candidate is the composer's own (unchanged) active body,
    while the changed ``name`` is made visible to the composer's composed-preset resolution through the
    candidate scope — so a composer the judged version breaks (its target can now ask with no
    reply/resume path, or reads a state nothing binds) is refused too. A composer route that yields
    lines is checked again outside the scope (the active state before the write): only lines the judged
    version introduces count against the write; lines already present there are logged at WARNING and
    refuse nothing. The scope is cleared on exit so the candidate never leaks into the commit.
    """
    targets = {name} | await composing_closure(name)
    routes = await routes_bound_to(targets)
    if not routes:
        return

    lines_with_candidate: dict[str, list[str]] = {}
    with candidate_bodies({name: candidate_body}):
        for route_name, route in sorted(routes.items()):
            if route.target_name == name:
                candidate = candidate_body
            else:
                candidate = await active_target_candidate_body(route.target_kind, route.target_name)
            lines = await target_bind_refusal_lines(route, candidate)
            if lines:
                lines_with_candidate[route_name] = lines

    broken: list[str] = []
    for route_name, lines in lines_with_candidate.items():
        route = routes[route_name]
        if route.target_name == name:
            broken.append(
                f"route {route_name!r}, bound to preset {name!r}: {write.judged} cannot bind it ({'; '.join(lines)})"
            )
            continue
        composer = route.target_name
        before = await target_bind_refusal_lines(route, await active_target_candidate_body(route.target_kind, composer))
        line = _composer_route_line(route_name, composer, name, write, lines, before)
        if line is not None:
            broken.append(line)
    if broken:
        raise ConflictError(
            f"{write.refusal_opening(name)} would leave conversation route(s) unable to bind: {'; '.join(broken)}"
        )


def _composer_route_line(
    route_name: str, composer: str, name: str, write: PresetWrite, lines: list[str], before: list[str]
) -> str | None:
    """The refusal line of a route bound to ``composer`` for the lines the judged version introduces, or ``None``.

    ``lines`` are the route's refusal lines with the judged version of ``name`` visible, ``before`` those
    of the active state. Lines present in both are logged at WARNING and refuse nothing.
    """
    already = [line for line in lines if line in before]
    introduced = [line for line in lines if line not in before]
    if already:
        logger.warning(
            "conversation route %r, bound to preset %r, could not bind before this write of preset %r; "
            "these lines were already present and do not refuse the write: %s",
            route_name,
            composer,
            name,
            "; ".join(already),
        )
    if not introduced:
        return None
    if already:
        return (
            f"route {route_name!r}, bound to preset {composer!r}: {write.judged} adds a reason preset "
            f"{composer!r} cannot bind ({'; '.join(introduced)})"
        )
    return (
        f"route {route_name!r}, bound to preset {composer!r}: preset {composer!r} would no longer bind "
        f"with {write.judged} ({'; '.join(introduced)})"
    )
