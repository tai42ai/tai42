"""The dependencies of one render, loaded before it begins: each id's source or its load failure.

A render reaches a dependency through the engine's loader, which answers from here. Loading ahead
never changes what the render does: a failure is answered only when the render reaches that id,
so a dependency the render never reaches fails nothing, exactly as when it is never read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from tai42_contract.template import TemplateBodyError

from tai42_skeleton.template.render_cache import RenderCache


class DependencyPreload:
    """The literal-name dependency closure of one render, loaded on the loop that awaits it.

    ``cache`` says which ids the engine already holds compiled or remembers absent, and parses a
    source for its dependency names; ``load`` reads a stored template's text by id, raising
    ``FileNotFoundError`` when it is not stored.
    """

    def __init__(self, cache: RenderCache, load: Callable[[str], Awaitable[str]]) -> None:
        """Start with nothing loaded."""
        self._cache = cache
        self._load = load
        self._sources: dict[str, str] = {}
        self._failures: dict[str, Exception] = {}

    async def load(self, dependencies: frozenset[str]) -> None:
        """Load the closure of ``dependencies``, level by level, each level's loads in flight together.

        An id the engine holds compiled or remembers absent is not loaded. A load failure is kept
        for the id and raised by :meth:`answer`; a not-stored id (``FileNotFoundError``) is also
        remembered absent, as the loader rules. A source that cannot be parsed is kept as it is
        (the engine raises its fault when the render reaches it) and adds no dependencies. A
        cancellation propagates and cancels the loads in flight.
        """
        names = dependencies
        while True:
            wanted = [name for name in sorted(names) if self._needs(name)]
            if not wanted:
                return
            async with asyncio.TaskGroup() as group:
                for name in wanted:
                    group.create_task(self._load_one(name))
            names = frozenset().union(*(self._dependencies_of(name) for name in wanted))

    def _needs(self, name: str) -> bool:
        return (
            name not in self._sources
            and name not in self._failures
            and name not in self._cache.absent
            and not self._cache.has_dependency(name)
        )

    async def _load_one(self, template_id: str) -> None:
        try:
            self._sources[template_id] = await self._load(template_id)
        except Exception as exc:
            if isinstance(exc, FileNotFoundError):
                self._cache.absent.add(template_id)
            self._failures[template_id] = exc

    def _dependencies_of(self, template_id: str) -> frozenset[str]:
        source = self._sources.get(template_id)
        if source is None:
            return frozenset()
        try:
            return self._cache.dependencies_of(template_id, source)
        except TemplateBodyError:
            # The engine parses the same source when the render reaches it and raises this fault there.
            return frozenset()

    def answer(self, template_id: str) -> str | None:
        """The loaded source of ``template_id``, or ``None`` when it was not loaded; a kept load failure raises."""
        failure = self._failures.get(template_id)
        if failure is not None:
            raise failure
        return self._sources.get(template_id)

    def forget(self, matches: Callable[[str], bool]) -> None:
        """Drop every loaded source and kept failure whose id ``matches``, so the render reads it fresh."""
        for loaded in (self._sources, self._failures):
            for template_id in [template_id for template_id in loaded if matches(template_id)]:
                loaded.pop(template_id, None)


__all__ = ["DependencyPreload"]
