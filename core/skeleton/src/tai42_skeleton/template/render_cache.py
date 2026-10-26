"""The render state a resource manager keeps from storage, and its eviction.

Four caches feed a render: stored templates compiled by id (a TTL/LRU cache when the settings
give it a positive size and TTL), inline sources compiled once by content, ids storage answered
"not found" for, and the engine's own cache of ``{% include %}``/``{% extends %}`` targets.
:class:`RenderCache` owns all four and drops what a store write made stale — one id, everything
under a prefix, or everything.
"""

import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from async_lru import alru_cache
from jinja2 import Environment
from jinja2 import Template as JinjaTemplate

from tai42_skeleton.template.absent_ids import AbsentTemplateIds
from tai42_skeleton.template.errors import TemplateNotFoundError
from tai42_skeleton.template.settings import TemplateCacheSettings

logger = logging.getLogger(__name__)


class RenderCache:
    """Compiled stored and inline templates, remembered absent ids, and engine-cached dependencies.

    ``env`` compiles every source and holds the engine's dependency cache; ``load`` reads a stored
    template's text by id, raising ``FileNotFoundError`` when it is not stored; ``settings`` bound
    the caches.
    """

    def __init__(self, env: Environment, load: Callable[[str], Awaitable[str]], settings: TemplateCacheSettings):
        """Build the caches from ``settings``; the compiled-template cache is off without a positive size and TTL."""
        self._env = env
        self._load = load

        # Compile-once cache for INLINE (content) templates. The stored-template
        # path is cached by id via ``_compiled``; without this an inline
        # condition/expr (e.g. a hook rendered on every fire) is re-parsed on
        # every render.
        self.inline: Callable[[str], JinjaTemplate] = lru_cache(maxsize=settings.max_size)(env.from_string)

        # Template ids currently compiled into the cache. Tracked so a delete can
        # evict just the affected key(s) instead of dropping the whole cache. It
        # stays bounded by what the cache actually holds: a delete prunes stale
        # ids, and ``_fetch_and_compile`` runs an amortized sweep whenever the set
        # has grown past the live cache size (an id the cache dropped on its own
        # via TTL/LRU), so the registry never outgrows the cache itself.
        self._tracked_ids: set[str] = set()

        # Only cache if we have BOTH storage space (>0) AND retention time (>0).
        # If ttl is 0, items expire instantly, making caching useless.
        self.enabled = (settings.max_size is None or settings.max_size > 0) and (
            settings.ttl is None or settings.ttl > 0
        )
        # Ids storage answered "not found" for (an absent locale variant or include target), so a
        # render does not re-read them; dropped by every eviction, so a platform write is seen at once.
        self.absent = AbsentTemplateIds(ttl=settings.ttl, max_size=settings.max_size, enabled=self.enabled)
        self._compiled: Callable[[str], Awaitable[JinjaTemplate]]
        if self.enabled:
            self._compiled = alru_cache(maxsize=settings.max_size, ttl=settings.ttl)(self._fetch_and_compile)
            logger.info(f"ResourceManager initialized with cache_size={settings.max_size} ttl={settings.ttl}")
        else:
            # Bypass cache wrapper entirely
            self._compiled = self._fetch_and_compile
            logger.info("ResourceManager initialized with caching disabled")

    async def _fetch_and_compile(self, template_id: str) -> JinjaTemplate:
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
            raise TemplateNotFoundError(f"Template '{template_id}' not found.") from exc

        if self.enabled:
            self._tracked_ids.add(template_id)
            # Amortized prune: the set only exceeds the cache's live entry count
            # when the cache has evicted ids on its own — LRU eviction under a
            # bounded max_size AND ttl expiry under an unbounded one — so drop any
            # tracked id the cache no longer holds. Runs only when there is
            # something to prune; between sweeps the set tracks the live cache size.
            cache_info = getattr(self._compiled, "cache_info", None)
            contains = getattr(self._compiled, "cache_contains", None)
            if cache_info is not None and contains is not None and len(self._tracked_ids) > cache_info().currsize:
                for tracked in list(self._tracked_ids):
                    if not contains(tracked):
                        self._tracked_ids.discard(tracked)
        return self._env.from_string(content)

    async def compiled(self, template_id: str) -> JinjaTemplate:
        """The compiled template ``template_id``; a not-found answer is remembered and raised again without a read.

        Only a not-found answer is remembered: a storage error raises as it is, every time.
        """
        if template_id in self.absent:
            raise TemplateNotFoundError(f"Template '{template_id}' not found.")
        try:
            return await self._compiled(template_id)
        except TemplateNotFoundError:
            self.absent.add(template_id)
            raise

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
        # ``_compiled`` is the alru_cache wrapper only when caching is enabled;
        # otherwise it's the bare method with no cache controls.
        cache_clear = getattr(self._compiled, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()
            self._tracked_ids.clear()
            logger.info("ResourceManager cache cleared.")
        if self._env.cache is not None:
            self._env.cache.clear()
        self.absent.forget(lambda _template_id: True)

    def evict(self, template_id: str) -> None:
        """Drop ``template_id`` from the compiled cache, the engine cache and the absent ids.

        Idempotent — an already-absent key is a no-op; the compiled-cache drop is a no-op
        when caching is off while the engine-cache drop always runs.
        """
        invalidate = getattr(self._compiled, "cache_invalidate", None)
        if invalidate is not None:
            invalidate(template_id)
        self._tracked_ids.discard(template_id)
        self._evict_engine_cache(lambda name: name == template_id)
        self.absent.forget(lambda tracked: tracked == template_id)

    def evict_prefix(self, prefix: str) -> None:
        """Drop every id under ``prefix`` from the compiled cache, the engine cache and the absent ids.

        Tracked ids that the compiled cache has already dropped (TTL/LRU) are pruned in the
        same pass, keeping the registry bounded by what is actually cached.
        """
        self._evict_engine_cache(lambda name: name.startswith(prefix))
        self.absent.forget(lambda tracked: tracked.startswith(prefix))
        invalidate = getattr(self._compiled, "cache_invalidate", None)
        if invalidate is None:
            # Compiled-template caching disabled: nothing is tracked by id.
            self._tracked_ids.clear()
            return
        contains = getattr(self._compiled, "cache_contains", None)
        for template_id in list(self._tracked_ids):
            if template_id.startswith(prefix):
                invalidate(template_id)
                self._tracked_ids.discard(template_id)
            elif contains is not None and not contains(template_id):
                self._tracked_ids.discard(template_id)

    def info(self) -> Any:
        """The compiled-template cache statistics, or ``None`` when caching is disabled."""
        cache_info = getattr(self._compiled, "cache_info", None)
        if cache_info is not None:
            return cache_info()
        return None
