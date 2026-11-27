"""The render state a resource manager keeps from storage, and its eviction.

Four caches feed a render: stored templates compiled by id (a TTL/LRU cache when the settings
give it a positive size and TTL), inline sources compiled once by content, ids storage answered
"not found" for, and the engine's own cache of ``{% include %}``/``{% extends %}`` targets.
:class:`RenderCache` owns all four and drops what a store write made stale — one id, everything
under a prefix, or everything.
"""

import logging
import weakref
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import NamedTuple

from jinja2 import Environment, meta, nodes
from jinja2 import Template as JinjaTemplate
from tai42_contract.template import TemplateBodyError, TemplateNotFoundError
from tai42_kit.utils.async_cache import AsyncCache

from tai42_skeleton.template.absent_ids import AbsentTemplateIds
from tai42_skeleton.template.settings import TemplateCacheSettings

logger = logging.getLogger(__name__)


class CompiledTemplate(NamedTuple):
    """A compiled template and the names of the templates it includes, extends or imports by a literal name.

    A dependency named by an expression is not among ``dependencies``: it is known only at render.
    """

    template: JinjaTemplate
    dependencies: frozenset[str]


class CompiledCacheInfo(NamedTuple):
    """How many compiled templates the cache holds now, and its bound (``None`` when unbounded)."""

    currsize: int
    maxsize: int | None


def _literal_dependencies(ast: nodes.Template) -> frozenset[str]:
    return frozenset(name for name in meta.find_referenced_templates(ast) if name is not None)


class RenderCache:
    """Compiled stored and inline templates, remembered absent ids, and engine-cached dependencies.

    ``env`` compiles every source and holds the engine's dependency cache; ``load`` reads a stored
    template's text by id, raising ``FileNotFoundError`` when it is not stored; ``settings`` bound
    the caches. A source that does not parse raises
    :class:`~tai42_contract.template.TemplateBodyError`; an id that is not stored raises
    :class:`~tai42_contract.template.TemplateNotFoundError`.
    """

    def __init__(self, env: Environment, load: Callable[[str], Awaitable[str]], settings: TemplateCacheSettings):
        """Build the caches from ``settings``; the compiled-template cache is off without a positive size and TTL."""
        self._env = env
        self._load = load

        # Compile-once cache for INLINE (content) templates. The stored-template
        # path is cached by id via ``_compiled``; without this an inline
        # condition/expr (e.g. a hook rendered on every fire) is re-parsed on
        # every render.
        self.inline: Callable[[str], CompiledTemplate] = lru_cache(maxsize=settings.max_size)(self._compile)

        # Only cache if we have BOTH storage space (>0) AND retention time (>0).
        # If ttl is 0, items expire instantly, making caching useless.
        self.enabled = (settings.max_size is None or settings.max_size > 0) and (
            settings.ttl is None or settings.ttl > 0
        )
        # Ids storage answered "not found" for (an absent locale variant or include target), so a
        # render does not re-read them; dropped by every eviction, so a platform write is seen at once.
        self.absent = AbsentTemplateIds(ttl=settings.ttl, max_size=settings.max_size, enabled=self.enabled)
        self._compiled: AsyncCache[str, CompiledTemplate] | None = None
        if self.enabled:
            self._compiled = AsyncCache(maxsize=settings.max_size, ttl=settings.ttl)
            logger.info(f"ResourceManager initialized with cache_size={settings.max_size} ttl={settings.ttl}")
        else:
            logger.info("ResourceManager initialized with caching disabled")

    async def _fetch_and_compile(self, template_id: str) -> CompiledTemplate:
        logger.debug(f"Fetching template '{template_id}'")
        try:
            content = await self._load(template_id)
        except FileNotFoundError as exc:
            # Genuinely missing -> raise clearly. ``Storage.load`` contractually
            # raises ``FileNotFoundError`` only for absent content; present-but-
            # empty content returns "" and is a valid template that renders to
            # empty, so it must NOT be treated as missing. Logged at debug, not error:
            # the raised ``TemplateNotFoundError`` is the loud signal, and a locale-variant
            # probe walks expected misses down the fallback chain.
            logger.debug(f"Template '{template_id}' lookup failed.")
            self.absent.add(template_id)
            raise TemplateNotFoundError(f"Template '{template_id}' not found.") from exc
        return self._compile(content)

    def _compile(self, source: str) -> CompiledTemplate:
        """Compile ``source`` in the manager's environment.

        Compiling reads nothing, so every fault it raises is the author's body fault.
        """
        try:
            ast = self._env.parse(source)
            return CompiledTemplate(self._env.from_string(ast), _literal_dependencies(ast))
        except Exception as exc:
            raise TemplateBodyError(str(exc)) from exc

    def dependencies_of(self, name: str, source: str) -> frozenset[str]:
        """The literal dependency names of the stored template ``name`` whose text is ``source``.

        A source that cannot be parsed is the author's body fault, as it is when the render reaches it.
        """
        try:
            return _literal_dependencies(self._env.parse(source, name=name))
        except Exception as exc:
            raise TemplateBodyError(str(exc)) from exc

    def has_dependency(self, name: str) -> bool:
        """Whether the engine holds the dependency ``name`` compiled, so a render resolves it without a read."""
        cache, loader = self._env.cache, self._env.loader
        # The engine keys each entry ``(weakref.ref(loader), name)``, as the eviction below reads it.
        return cache is not None and loader is not None and (weakref.ref(loader), name) in cache

    async def compiled(self, template_id: str) -> CompiledTemplate:
        """The compiled template ``template_id``; a not-found answer is remembered and raised again without a read.

        Only a not-found answer is remembered: a storage error raises as it is, every time.
        """
        if template_id in self.absent:
            raise TemplateNotFoundError(f"Template '{template_id}' not found.")
        if self._compiled is None:
            return await self._fetch_and_compile(template_id)
        return await self._compiled.get_or_load(template_id, lambda: self._fetch_and_compile(template_id))

    def _evict_engine_cache(self, matches: Callable[[str], bool]) -> None:
        """Drop every engine-cached dependency template whose name ``matches``.

        ``{% include %}`` and ``{% extends %}`` resolve their target through the
        loader into the engine's own template cache, keyed by name and separate
        from the compiled-template cache keyed by id. The loader serves stored
        text with no up-to-date signal, so a target re-uploaded under the same
        name stays served from this cache until the process restarts unless its
        entry is dropped when the source changes. Removing the matching entries here
        makes the next render reload the new source. A top-level render compiles via
        ``from_string`` and never populates this cache, so only dependency entries
        are affected.

        The engine cache is always present (independent of the compiled-template
        cache), so this runs regardless of whether compiled caching is enabled.
        """
        cache = self._env.cache
        if cache is None:
            return
        # The engine keys each entry ``(weakref.ref(loader), name)``; match on the
        # name and delete the live entries that match.
        for key in list(cache.keys()):
            if matches(key[1]):
                del cache[key]

    def clear(self) -> None:
        """Drop every compiled template, every engine-cached dependency and every remembered absent id.

        The compiled-template clear is a no-op when that cache is off; the engine's
        dependency cache (``{% include %}``/``{% extends %}`` targets) is always
        present and is cleared whole, so no re-uploaded dependency is served stale.
        """
        if self._compiled is not None:
            self._compiled.clear()
            logger.info("ResourceManager cache cleared.")
        if self._env.cache is not None:
            self._env.cache.clear()
        self.absent.forget(lambda _template_id: True)

    def evict(self, template_id: str) -> None:
        """Drop ``template_id`` from the compiled cache, the engine cache and the absent ids.

        Idempotent — an already-absent key is a no-op; the compiled-cache drop is a no-op
        when caching is off while the engine-cache drop always runs.
        """
        if self._compiled is not None:
            self._compiled.invalidate(template_id)
        self._evict_engine_cache(lambda name: name == template_id)
        self.absent.forget(lambda tracked: tracked == template_id)

    def evict_prefix(self, prefix: str) -> None:
        """Drop every id under ``prefix`` from the compiled cache, the engine cache and the absent ids."""
        self._evict_engine_cache(lambda name: name.startswith(prefix))
        self.absent.forget(lambda tracked: tracked.startswith(prefix))
        if self._compiled is not None:
            self._compiled.evict(lambda template_id: template_id.startswith(prefix))

    def info(self) -> CompiledCacheInfo | None:
        """The compiled-template cache's live size and bound, or ``None`` when caching is disabled."""
        if self._compiled is None:
            return None
        return CompiledCacheInfo(currsize=self._compiled.currsize, maxsize=self._compiled.maxsize)
