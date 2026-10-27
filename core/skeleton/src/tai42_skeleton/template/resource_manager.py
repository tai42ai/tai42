"""Fetch, render, schema-infer, and cache stored (and inline) templates.

:class:`ResourceManager` is the resource feature's one entry point. It renders here, in the
sandboxed engine whose ``{% include %}``/``{% extends %}`` dependencies resolve through one event
loop per render, and composes the rest: the render caches and their eviction
(:mod:`~tai42_skeleton.template.render_cache`), the subject's locale
(:mod:`~tai42_skeleton.template.render_locale`), content and image loading
(:mod:`~tai42_skeleton.template.sources`), store writes and deletes
(:mod:`~tai42_skeleton.template.store_writes`) and input-schema inference
(:mod:`~tai42_skeleton.template.input_schema`).
"""

import asyncio
import itertools
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from jinja2 import FunctionLoader
from tai42_contract.storage import Storage
from tai42_contract.template import ImageReference, TemplatedText, TemplateEviction
from tai42_kit.clients import shutdown_all_clients

from tai42_skeleton.template.errors import TemplateNotFoundError, TemplateNotTextError
from tai42_skeleton.template.input_schema import template_input_schema, undeclared_variables
from tai42_skeleton.template.media import MediaBlock
from tai42_skeleton.template.path_guard import safe_template_path
from tai42_skeleton.template.reads import RecordingEnvironment, record_template_read
from tai42_skeleton.template.render_cache import RenderCache
from tai42_skeleton.template.render_locale import compiled_for_locale, list_format, locale_variant_ids, with_locale
from tai42_skeleton.template.settings import template_cache_settings
from tai42_skeleton.template.sources import SourceLoader
from tai42_skeleton.template.store_writes import StoreWrites

logger = logging.getLogger(__name__)

#: The process-wide source of :attr:`ResourceManager.epoch`: never reset, so every manager built in
#: this process (at boot or after a reload) carries a value no earlier manager had.
_EPOCHS = itertools.count(1)


class ResourceManager:
    """Fetch, render, schema-infer, and cache stored (and inline) templates, guarded and locale-aware.

    ``epoch`` identifies this manager among every manager the process builds; ``generation`` counts
    its local cache evictions. A cache of renders made through a manager keys on both, so a rebuilt
    manager or an evicted stored template re-renders. Every eviction seam ends by calling
    ``on_evicted`` with what it dropped, after every cache of the manager is dropped.
    """

    def __init__(self, provider: Storage | None, *, on_evicted: Callable[[TemplateEviction], None]):
        """Wire the sandboxed Jinja environment and the render caches from settings.

        ``provider`` is the Storage backend, or ``None`` when none is registered; ``on_evicted``
        receives every eviction this manager applies.
        """
        self._provider = provider
        self._on_evicted = on_evicted
        self.epoch: int = next(_EPOCHS)
        self.generation: int = 0
        # Sandboxed engine: template text is an authoring surface (hook
        # condition/expr, stored/inline renders), so rendering blocks dunder and
        # unsafe-callable access — a traversal like
        # ``{{ ''.__class__.__mro__[1].__subclasses__() }}`` raises SecurityError
        # instead of reaching host primitives. Normal ``{{ var }}``, filters, and
        # ``{% include %}`` are unaffected.
        self._env = RecordingEnvironment(loader=FunctionLoader(self._sync_loader))
        # The locale-aware list formatter is available to every rendered template; it reads
        # the render's resolved locale from the reserved context key, never a template arg.
        self._env.filters["list_format"] = list_format

        # Per-render event loop bound to the rendering thread so every
        # {% include %}/{% extends %} dependency resolves through one loop — hence
        # one storage client per render instead of one opened+closed per include.
        self._render_loop = threading.local()

        self._render_cache = RenderCache(self._env, self._load_by_id, template_cache_settings())
        self._sources = SourceLoader(lambda: self.provider)
        self._store_writes = StoreWrites(lambda: self.provider, evict_id=self.evict_compiled, evict_dir=self.evict_dir)

    @property
    def cache_enabled(self) -> bool:
        """Whether this manager caches compiled templates (a positive size and TTL)."""
        return self._render_cache.enabled

    @property
    def provider(self) -> Storage:
        """The active Storage provider, or a loud failure when none is registered.

        The skeleton ships no backend (dead by default): an unconfigured manager
        must never silently no-op, so every use that needs storage raises here.
        """
        if self._provider is None:
            raise RuntimeError(
                "ResourceManager has no Storage provider registered. The skeleton "
                "ships no storage backend (dead by default); register one via "
                "tai42_app.storage.register_storage before fetching or rendering a "
                "stored resource."
            )
        return self._provider

    @contextmanager
    def _render_scope(self) -> Iterator[None]:
        """Mark this render thread as inside a render so every dependency shares one event loop.

        The storage client opened for the first {% include %}/{% extends %} is reused and
        closed once when the scope exits, instead of a fresh loop + connection per
        dependency.

        The loop is created lazily by ``_sync_loader`` on the first dependency, so an
        include-free render creates no loop at all.
        """
        self._render_loop.active = True
        self._render_loop.loop = None
        try:
            yield
        finally:
            self._render_loop.active = False
            loop = self._render_loop.loop
            self._render_loop.loop = None
            if loop is not None:
                # Close the render loop's pooled clients once. A cleanup failure is
                # logged loudly but must not mask the render's result/exception.
                try:
                    loop.run_until_complete(shutdown_all_clients())
                except Exception:
                    logger.exception("Error closing pooled clients after template render")
                loop.close()

    async def _load_by_id(self, template_id: str) -> str:
        """Read a template by its bare storage id, root-contained.

        Every by-id read — render/fetch/schema/``{% include %}``, not only the media
        ``load`` seam — funnels through here so the id (a storage path) is passed
        through :func:`safe_template_path` before it reaches the provider. That is the
        app-layer defense against traversal for a backend that does not self-guard: an
        id like ``../../x`` (from an untrusted backup or an LLM-driven tool) raises
        :class:`~tai42_skeleton.template.path_guard.UnsafeTemplatePathError` instead of
        reading outside the template root.

        A stored object that is not UTF-8 text raises :class:`TemplateNotTextError`
        naming ``template_id``: it exists, but it is not a template.
        """
        safe_template_path(template_id)
        try:
            return await self.provider.load(template_id)
        except UnicodeDecodeError as exc:
            raise TemplateNotTextError(
                f"Template '{template_id}' is not a text template: the stored object is not UTF-8 text."
            ) from exc

    def _sync_loader(self, template_id: str) -> str | None:
        """Synchronous bridge for Jinja2's FunctionLoader, fetching a dependency from async storage.

        Lets {% extends %} and {% include %} fetch their dependency from async storage.

        A genuinely missing dependency becomes ``None`` so Jinja raises
        ``TemplateNotFound`` (keeping ``{% include ... ignore missing %}``
        working) and is remembered absent, so the next render answers ``None`` without a
        read; every other failure propagates loudly instead of being mistaken for a
        missing template, and is never remembered.
        """
        absent = self._render_cache.absent
        if template_id in absent:
            return None
        if getattr(self._render_loop, "active", False):
            # Inside a render scope (the normal path): reuse the per-render loop so
            # sibling includes share one storage client. Create it on first use.
            loop = self._render_loop.loop
            if loop is None:
                loop = asyncio.new_event_loop()
                self._render_loop.loop = loop
            try:
                return loop.run_until_complete(self._load_by_id(template_id))
            except FileNotFoundError:
                absent.add(template_id)
                return None

        # Standalone fallback (no active render scope): a one-shot loop that
        # closes its own pooled client, mirroring the render-scope teardown.
        async def _load() -> str | None:
            try:
                return await self._load_by_id(template_id)
            finally:
                try:
                    await shutdown_all_clients()
                except Exception:
                    logger.exception("Error closing pooled clients after template load")

        try:
            return asyncio.run(_load())
        except FileNotFoundError:
            absent.add(template_id)
            return None

    async def fetch_template(self, template_id: str) -> str:
        """Return the raw (uncompiled) template text for ``template_id``.

        A missing template raises :class:`TemplateNotFoundError` (mapped to 404 by
        the HTTP surface) rather than leaking storage's ``FileNotFoundError``.
        """
        try:
            return await self._load_by_id(template_id)
        except FileNotFoundError as exc:
            raise TemplateNotFoundError(f"Template '{template_id}' not found.") from exc

    async def load(self, source: str, *, with_mime: bool = True) -> tuple[bytes, str | None]:
        """Resolve ``source`` — an ``http(s)`` URL, a ``data:`` URI or a bare storage id — to ``(bytes, mime)``.

        The source kind is decided by its scheme, never by a fallback; any other scheme or an
        id containing ``://`` raises. A storage id is root-contained before it reaches the
        provider. ``with_mime=False`` skips the storage mime lookup and returns ``None`` for
        it. See :meth:`SourceLoader.load <tai42_skeleton.template.sources.SourceLoader.load>`.
        """
        return await self._sources.load(source, with_mime=with_mime)

    async def load_stored(self, path: str, *, with_mime: bool = True) -> tuple[bytes, str | None]:
        """Read the stored object at storage id ``path`` as ``(bytes, mime)``, root-contained.

        ``path`` is always a storage id, never a URL; ``with_mime`` is as in :meth:`load`.
        """
        return await self._sources.load_stored(path, with_mime=with_mime)

    async def load_file(self, source: str) -> str | MediaBlock:
        """Load ``source`` and decode it to extracted text OR a ``MediaBlock``.

        Bytes that cannot be loaded raise :class:`ContentNotLoadableError`; without the
        ``files`` extra it raises the ``501`` ``NotSupportedError`` before any read. See
        :meth:`SourceLoader.load_file <tai42_skeleton.template.sources.SourceLoader.load_file>`.
        """
        return await self._sources.load_file(source)

    async def resolve_image(self, source: str | bytes) -> ImageReference:
        """Resolve an image reference to an addressable image with its asserted ``image/*`` mime.

        A public URL or a ``data:`` URI passes through; a storage id or raw ``bytes`` becomes a
        base64 ``data:`` URI. A non-image raises. See
        :meth:`SourceLoader.resolve_image <tai42_skeleton.template.sources.SourceLoader.resolve_image>`.
        """
        return await self._sources.resolve_image(source)

    def clear_cache(self) -> None:
        """Drop every compiled template and every engine-cached dependency.

        The compiled-template clear is a no-op when that cache is off; the engine's
        dependency cache (``{% include %}``/``{% extends %}`` targets) is always
        present and is cleared whole, so no re-uploaded dependency is served stale.
        """
        self.generation += 1
        self._render_cache.clear()
        self._on_evicted(TemplateEviction(path=None))

    def evict_compiled(self, template_id: str) -> None:
        """Evict a single template from the compiled cache and the engine cache.

        The public eviction seam: a store write replaces content the compiled cache
        keys by id, so both the local write paths and a fleet ``evict_template`` op
        applied on a sibling worker drop the one stale key here. The engine-cached
        include/extends target of the same name is dropped too, so a re-uploaded
        dependency is reloaded on the next render. Idempotent — an already-absent
        key is a no-op; the compiled-cache drop is a no-op when caching is off while
        the engine-cache drop always runs.
        """
        self.generation += 1
        self._render_cache.evict(template_id)
        self._on_evicted(TemplateEviction(path=template_id))

    def evict_dir(self, path: str) -> None:
        """Evict every compiled template under ``path/``.

        The directory counterpart of :meth:`evict_compiled`, applying the provider's
        prefix semantics (everything under ``path/``): a dir delete on this worker and
        a fleet ``evict_template`` op carrying a directory key both drop every stale
        compilation under the prefix through this one seam. Idempotent.
        """
        self.generation += 1
        self._render_cache.evict_prefix(path.rstrip("/") + "/")
        self._on_evicted(TemplateEviction(path=path.rstrip("/"), prefix=True))

    def get_cache_info(self) -> Any:
        """Return the compiled-template cache statistics, or ``None`` when caching is disabled."""
        return self._render_cache.info()

    async def render_by_id(
        self, template_id: str, kwargs: dict[str, Any] | None = None, locale: str | None = None
    ) -> str:
        """Render the stored template ``template_id`` for ``locale`` with ``kwargs`` as its context.

        Every id of the locale probe chain is recorded as read before it resolves, so a later
        write of a variant that is missing now is seen by a reads recorder too.
        """
        probes = [template_id] if locale is None else locale_variant_ids(template_id, locale)
        for candidate in probes:
            record_template_read(candidate)
        template = await compiled_for_locale(template_id, locale, self._render_cache.compiled)
        context = with_locale(kwargs or {}, locale)

        def _render() -> str:
            with self._render_scope():
                return template.render(**context)

        return await asyncio.to_thread(_render)

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        """Render ``text`` — its stored resource by ``id``, else its inline ``content``.

        Rendered with its own ``kwargs`` as the render context.

        A stored id resolves through :meth:`render_by_id` (locale variants included), so a
        missing one raises ``TemplateNotFoundError``; a template the engine cannot render
        raises out of Jinja. ``locale`` also reaches an inline render, as the language the
        ``list_format`` filter formats in.
        """
        if text.id is not None:
            return await self.render_by_id(text.id, text.kwargs, locale=locale)

        content = text.content
        # A templated text carries exactly one source, so with no id the content is a real
        # string (present-but-empty included — an empty template renders to an empty string).
        if content is None:
            raise AssertionError
        context = with_locale(text.kwargs, locale)

        def _parse_and_render() -> str:
            with self._render_scope():
                return self._render_cache.inline(content).render(**context)

        return await asyncio.to_thread(_parse_and_render)

    async def _template_source(self, content: str | None, template_id: str | None) -> str:
        """The template source a read inspects: inline ``content``, or the stored ``template_id``.

        Exactly one of the two must be given. A missing ``template_id`` raises
        ``TemplateNotFoundError``; a present-but-empty stored template is a valid (empty)
        source rather than rejected.
        """
        if content is not None and template_id is not None:
            raise ValueError("Provide either 'content' OR 'template_id', not both.")
        if content is not None:
            return content
        if template_id is None:
            raise ValueError("You must provide either a template or a template_id.")
        try:
            return await self._load_by_id(template_id)
        except FileNotFoundError as exc:
            raise TemplateNotFoundError(f"Template '{template_id}' not found.") from exc

    async def get_template_schema(self, content: str | None = None, template_id: str | None = None) -> dict[str, Any]:
        """Return the JSON-Schema description of a template's inputs, from ``content`` or a stored ``template_id``.

        The schema is :func:`~tai42_skeleton.template.input_schema.template_input_schema` over this
        manager's rendering environment: reading a renderable template never fails. Raises
        ``TemplateNotFoundError`` for a missing ``template_id`` and ``jinja2.TemplateError`` only when
        the template itself is broken (the app's own environment refuses to parse it) — an author
        error, not a read failure.
        """
        source = await self._template_source(content, template_id)
        return await asyncio.to_thread(template_input_schema, self._env, source)

    async def find_undeclared_variables(self, content: str | None = None, template_id: str | None = None) -> set[str]:
        """The undeclared variables in a template, from inline ``content`` or a stored ``template_id``."""
        source = await self._template_source(content, template_id)
        return await asyncio.to_thread(undeclared_variables, self._env, source)

    async def upload_template(self, path: str, content: str) -> None:
        """Store ``content`` at ``path`` and evict its compiled entry, so the next render reads it."""
        await self._store_writes.upload_template(path, content)

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Store raw ``data`` at ``path`` with ``content_type`` and evict its compiled entry.

        The bytes twin of :meth:`upload_template`. A text-only provider refuses bytes
        that are not UTF-8 with ``UnicodeDecodeError``, which propagates.
        """
        await self._store_writes.upload_bytes(path, data, content_type)

    async def list_resources(self, directory: str | None = None) -> list[str]:
        """List every stored resource path in the storage provider, or only those beneath ``directory``.

        A ``directory`` is listed by the provider itself (``Storage.list(directory)``), so
        only that directory's objects are read; a blank one raises ``ValueError``.
        """
        return await self.provider.list(directory)

    async def delete_object(self, path: str) -> None:
        """Delete the stored object at ``path`` unless a stored object still names it.

        Every delete of a stored object goes through here or :meth:`delete_template_dir`.
        A held object raises the ``409`` naming its holders and deletes nothing; a holder
        that cannot be read raises a ``500``; a missing object raises ``FileNotFoundError``.
        """
        await self._store_writes.delete_object(path)

    async def delete_template(self, path: str) -> None:
        """Delete the stored template at ``path`` and evict its compiled entry.

        Idempotent: an already-absent template is a no-op. A held template is refused by
        :meth:`delete_object`.
        """
        await self._store_writes.delete_template(path)

    async def delete_template_dir(self, path: str) -> None:
        """Delete every stored template under ``path/`` and evict their compiled entries.

        A ``path`` naming the store root raises ``ValueError`` before any read. Only the
        directory's objects are listed (``Storage.list(path)``), never the whole store.
        Refused, deleting and evicting nothing, when a stored object names any object under
        ``path/``. Eviction runs even on a partial-delete failure.
        """
        await self._store_writes.delete_template_dir(path)
