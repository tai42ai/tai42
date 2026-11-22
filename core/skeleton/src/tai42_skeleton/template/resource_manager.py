"""Fetch, render, schema-infer, and cache stored (and inline) templates.

:class:`ResourceManager` is the resource feature's one entry point. It renders here, in the
sandboxed engine whose ``{% include %}``/``{% extends %}`` dependencies load on the loop that
awaits the render, and composes the rest: the render caches and their eviction
(:mod:`~tai42_skeleton.template.render_cache`), the subject's locale
(:mod:`~tai42_skeleton.template.render_locale`), content and image loading
(:mod:`~tai42_skeleton.template.sources`), store writes and deletes
(:mod:`~tai42_skeleton.template.store_writes`) and input-schema inference
(:mod:`~tai42_skeleton.template.input_schema`).
"""

import asyncio
import itertools
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from jinja2 import FunctionLoader, TemplateNotFound
from jinja2 import Template as JinjaTemplate
from tai42_contract.storage import Storage
from tai42_contract.template import (
    ImageReference,
    TemplateBodyError,
    TemplatedText,
    TemplateEviction,
    TemplateNotFoundError,
    TemplateNotTextError,
    TemplateRenderError,
)
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.template.input_schema import template_input_schema, undeclared_variables
from tai42_skeleton.template.media import MediaBlock
from tai42_skeleton.template.path_guard import UnsafeTemplatePathError, safe_template_path
from tai42_skeleton.template.reads import RecordingEnvironment, record_template_read
from tai42_skeleton.template.render_cache import RenderCache
from tai42_skeleton.template.render_locale import compiled_for_locale, list_format, locale_variant_ids, with_locale
from tai42_skeleton.template.settings import template_cache_settings
from tai42_skeleton.template.sources import SourceLoader
from tai42_skeleton.template.store_writes import StoreWrites

#: The process-wide source of :attr:`ResourceManager.epoch`: never reset, so every manager built in
#: this process (at boot or after a reload) carries a value no earlier manager had.
_EPOCHS = itertools.count(1)


def _guarded_id(template_id: str) -> str:
    """``template_id`` when it is a canonical store key; an id the path guard refuses names no stored template."""
    try:
        return safe_template_path(template_id)
    except UnsafeTemplatePathError as exc:
        raise TemplateNotFoundError(str(exc)) from exc


def _refusal(template_id: str | None, exc: TemplateRenderError, *, rendering: bool) -> TemplateRenderError:
    """``exc`` reworded as the author-facing refusal of the text it was raised for.

    ``template_id`` is the text's stored id, ``None`` for inline content; ``rendering`` says the
    stored text was fetched and compiled and its render had begun, so ``exc`` came from the render
    (its includes included). A stored id that yields no template text "could not be fetched"; a
    stored text that does not parse (a :class:`TemplateBodyError` before the render) or does not
    render "could not be rendered"; an inline body "is not a renderable template"; ``exc``'s own
    message closes it.
    """
    if template_id is None:
        predicate = f"is not a renderable template: {exc}"
    elif rendering or isinstance(exc, TemplateBodyError):
        predicate = f"references stored id {template_id!r}, which could not be rendered: {exc}"
    else:
        predicate = f"references stored id {template_id!r}, which could not be fetched: {exc}"
    return type(exc)(predicate)


class ResourceManager:
    """Fetch, render, schema-infer, and cache stored (and inline) templates, guarded and locale-aware.

    ``epoch`` identifies this manager among every manager the process builds; ``generation`` counts
    its local cache evictions. A cache of renders made through a manager keys on both, so a rebuilt
    manager or an evicted stored template re-renders. Every eviction seam ends by calling
    ``on_evicted`` with what it dropped, after every cache of the manager is dropped.
    """

    def __init__(
        self,
        provider: Storage | None,
        *,
        on_evicted: Callable[[TemplateEviction], None],
        held_by: Callable[[frozenset[str]], Awaitable[list[ReferenceHolder]]],
    ):
        """Wire the sandboxed Jinja environment and the render caches from settings.

        ``provider`` is the Storage backend, or ``None`` when none is registered; ``on_evicted``
        receives every eviction this manager applies; ``held_by`` answers every holder of a set
        of stored ids, the check every delete reads first.
        """
        self._provider = provider
        self._on_evicted = on_evicted
        self._held_by = held_by
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

        # The render a rendering thread runs, with the loop that drives it: every
        # {% include %}/{% extends %} dependency loads on that loop, through its pooled
        # storage client. ``loader_fault`` holds the exception the loader let through during
        # the current render, which is never the author's.
        self._render_loop = threading.local()

        self._render_cache = RenderCache(self._env, self._load_by_id, template_cache_settings())
        self._sources = SourceLoader(lambda: self.provider)
        self._store_writes = StoreWrites(
            lambda: self.provider, held_by=self._held_by, evict_id=self.evict_compiled, evict_dir=self.evict_dir
        )

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
    def _render_scope(self, loop: asyncio.AbstractEventLoop) -> Iterator[None]:
        """Mark this render thread as inside a render whose dependencies load on ``loop``.

        ``loop`` is the loop that awaits the render: the render runs on a thread of its own,
        and each {% include %}/{% extends %} dependency is loaded on ``loop`` while the
        thread waits for it. The loader's fault mark is cleared on entry and exit, so a pooled
        thread never carries one into its next render.
        """
        self._render_loop.active = True
        self._render_loop.loop = loop
        self._render_loop.loader_fault = None
        try:
            yield
        finally:
            self._render_loop.active = False
            self._render_loop.loop = None
            self._render_loop.loader_fault = None

    async def _render(self, compile_template: Callable[[], JinjaTemplate], context: dict[str, Any]) -> str:
        """Compile and render on a thread of its own, sorting what fails into the author's fault or not.

        The only code other than the author's body that runs during the render is the loader
        (:meth:`_sync_loader`), which marks every fault it lets through: a marked exception
        propagates as itself. A :class:`TemplateRenderError` passes as raised; Jinja's
        ``TemplateNotFound`` for an include becomes :class:`TemplateNotFoundError`; anything else
        the body raises becomes :class:`TemplateBodyError`.
        """
        loop = asyncio.get_running_loop()

        def _compile_and_render() -> str:
            with self._render_scope(loop):
                try:
                    return compile_template().render(**context)
                except Exception as exc:
                    if exc is self._render_loop.loader_fault or isinstance(exc, TemplateRenderError):
                        raise
                    if isinstance(exc, TemplateNotFound):
                        raise TemplateNotFoundError(f"its include {exc.name!r} is not stored") from exc
                    raise TemplateBodyError(str(exc)) from exc

        return await asyncio.to_thread(_compile_and_render)

    async def _load_by_id(self, template_id: str) -> str:
        """Read a template by its bare storage id, root-contained.

        Every by-id read — render/fetch/schema/``{% include %}``, not only the media
        ``load`` seam — funnels through here so the id (a storage path) is passed
        through :func:`safe_template_path` before it reaches the provider. That is the
        app-layer defense against traversal for a backend that does not self-guard: an
        id like ``../../x`` (from an untrusted backup or an LLM-driven tool) raises
        :class:`~tai42_contract.template.TemplateNotFoundError` with the guard's reason instead
        of reading outside the template root.

        A stored object that is not UTF-8 text raises
        :class:`~tai42_contract.template.TemplateNotTextError` naming ``template_id``: it exists,
        but it is not a template.
        """
        _guarded_id(template_id)
        try:
            return await self.provider.load(template_id)
        except UnicodeDecodeError as exc:
            raise TemplateNotTextError(
                f"Template '{template_id}' is not a text template: the stored object is not UTF-8 text."
            ) from exc

    def _sync_loader(self, template_id: str) -> str | None:
        """Synchronous bridge for Jinja2's FunctionLoader, fetching a dependency from async storage.

        Lets {% extends %} and {% include %} fetch their dependency from async storage: called
        on the render's thread, it loads the dependency on the loop that drives the render
        (:meth:`_render_scope`) and waits for it. Called outside a render it raises
        ``RuntimeError``.

        A genuinely missing dependency becomes ``None`` so Jinja raises
        ``TemplateNotFound`` (keeping ``{% include ... ignore missing %}``
        working) and is remembered absent, so the next render answers ``None`` without a
        read; every other failure propagates loudly instead of being mistaken for a
        missing template, and is never remembered. A fault that is not a
        :class:`TemplateRenderError` is marked as the loader's for :meth:`_render`.
        """
        absent = self._render_cache.absent
        if template_id in absent:
            return None
        if not getattr(self._render_loop, "active", False):
            raise RuntimeError(
                f"template dependency '{template_id}' was loaded outside a render: a dependency loads only "
                "while render_by_id or render_templated_text drives the render"
            )
        try:
            return asyncio.run_coroutine_threadsafe(self._load_by_id(template_id), self._render_loop.loop).result()
        except FileNotFoundError:
            absent.add(template_id)
            return None
        except TemplateRenderError:
            raise
        except Exception as exc:
            self._render_loop.loader_fault = exc
            raise

    async def fetch_template(self, template_id: str) -> str:
        """Return the raw (uncompiled) template text for ``template_id``.

        A missing template raises :class:`~tai42_contract.template.TemplateNotFoundError` rather
        than leaking storage's ``FileNotFoundError``.
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
        ``files`` extra it raises :class:`~tai42_skeleton.template.errors.FilesNotConfiguredError`
        before any read. See
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
        write of a variant that is missing now is seen by a reads recorder too. A template that
        cannot be rendered as authored raises a :class:`TemplateRenderError` whose text names
        ``template_id``; every other failure propagates as itself.
        """
        try:
            guarded = _guarded_id(template_id)
            probes = [guarded] if locale is None else locale_variant_ids(guarded, locale)
            for candidate in probes:
                record_template_read(candidate)
            template = await compiled_for_locale(template_id, locale, self._render_cache.compiled)
        except TemplateRenderError as exc:
            raise _refusal(template_id, exc, rendering=False) from exc
        try:
            context = with_locale(kwargs or {}, locale)
            return await self._render(lambda: template, context)
        except TemplateRenderError as exc:
            raise _refusal(template_id, exc, rendering=True) from exc

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        """Render ``text`` — its stored resource by ``id``, else its inline ``content``.

        Rendered with its own ``kwargs`` as the render context.

        A stored id resolves through :meth:`render_by_id` (locale variants included). A text that
        cannot be rendered as authored raises a :class:`TemplateRenderError` (a missing id or
        include, a stored item that is not text, a body that does not parse or render) whose text
        names the stored id or says the text is inline; every other failure (storage, the network,
        an unconfigured manager) propagates as itself. ``locale`` also reaches an inline render, as
        the language the ``list_format`` filter formats in.
        """
        if text.id is not None:
            return await self.render_by_id(text.id, text.kwargs, locale=locale)

        content = text.content
        # A templated text carries exactly one source, so with no id the content is a real
        # string (present-but-empty included — an empty template renders to an empty string).
        if content is None:
            raise AssertionError
        try:
            context = with_locale(text.kwargs, locale)
            return await self._render(lambda: self._render_cache.inline(content), context)
        except TemplateRenderError as exc:
            raise _refusal(None, exc, rendering=False) from exc

    async def _template_source(self, content: str | None, template_id: str | None) -> str:
        """The template source a read inspects: inline ``content``, or the stored ``template_id``.

        Exactly one of the two must be given. A missing ``template_id`` raises
        :class:`~tai42_contract.template.TemplateNotFoundError`; a present-but-empty stored template is a valid (empty)
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
        A held object raises :class:`~tai42_skeleton.template.errors.StoredObjectHeldError`
        naming its holders and deletes nothing; a holder that cannot be read raises
        :class:`~tai42_skeleton.template.errors.HolderUnreadableError`; a missing object raises
        ``FileNotFoundError``.
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
        Refused with :class:`~tai42_skeleton.template.errors.StoredObjectHeldError`, deleting
        and evicting nothing, when a stored object names any object under ``path/``. Eviction
        runs even on a partial-delete failure. A failure once the provider's deletion may have
        begun is raised as :class:`~tai42_skeleton.template.errors.PartialDirectoryDeleteError`
        with the provider's failure as its cause; the provider's own pre-deletion
        ``FileNotFoundError``/``ValueError`` propagate as they are.
        """
        await self._store_writes.delete_template_dir(path)
