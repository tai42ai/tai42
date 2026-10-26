"""Resolve a content source — an ``http(s)`` URL, a ``data:`` URI or a storage id — to bytes, a file or an image.

:class:`SourceLoader` is the one resolver every content read funnels through, so the storage id of
every storage read is root-contained at one seam. The storage is resolved only when a storage id
is read: a URL or a ``data:`` URI loads with no storage registered.
"""

import asyncio
import mimetypes
from collections.abc import Callable
from urllib.parse import urlsplit

from tai42_contract.storage import Storage
from tai42_contract.template import ImageReference
from tai42_kit.net import fetch_url

from tai42_skeleton.template.data_uri import assert_image, data_uri, decode_data_uri
from tai42_skeleton.template.media import MediaBlock
from tai42_skeleton.template.path_guard import safe_template_path


def _names_storage_id(source: str) -> bool:
    """Whether ``source`` is a bare storage id: no URL scheme and no embedded ``://``."""
    return urlsplit(source).scheme == "" and "://" not in source


class SourceLoader:
    """Load a source as ``(bytes, mime)``, as extracted text or media, or as an addressable image.

    ``storage`` returns the active Storage provider, raising when none is registered.
    """

    def __init__(self, storage: Callable[[], Storage]) -> None:
        """Read storage ids through the provider ``storage`` returns."""
        self._storage = storage

    async def load(self, source: str, *, with_mime: bool = True) -> tuple[bytes, str | None]:
        """Resolve ``source`` to ``(bytes, mime)`` — the unified content resolver.

        The source kind is disambiguated UP FRONT by ``urlsplit(source).scheme``,
        never by a try/fallback (which would swallow storage's loud
        ``FileNotFoundError``):

        - ``http`` / ``https`` -> fetched over the SSRF-pinned :func:`fetch_url`.
        - ``data`` -> the inline ``data:`` URI is decoded (no network/storage).
        - empty scheme -> a storage ``id``/path (``Storage.load_object``, the bytes
          and the mime in one read). The empty-scheme branch runs the
          bare id through :func:`safe_template_path` first, enforcing root
          containment on the id (an ``UnsafeTemplatePathError`` propagates loudly),
          so every read that funnels here is guarded at this one seam.

        Any other scheme (``file:``, ``ftp:``) or an id containing ``://`` is
        ambiguous and raises loudly rather than being guessed. Each branch's own
        errors propagate.

        ``with_mime=False`` reads only the bytes (``Storage.load_bytes``) and returns
        ``mime`` as ``None`` for a storage id — for a caller that re-detects the
        type itself, so a backend whose ``load_object`` needs a second read for the
        type (the contract default, a ``stat``) makes none. http/data sources carry
        their mime inline, so the flag is a no-op for them.
        """
        if _names_storage_id(source):
            return await self.load_stored(source, with_mime=with_mime)
        scheme = urlsplit(source).scheme
        if scheme in ("http", "https"):
            return await fetch_url(source)
        if scheme == "data":
            return decode_data_uri(source)
        raise ValueError(
            f"Cannot resolve source {source!r}: only http(s) URLs, data URIs, and bare "
            "storage ids are supported (a file:/ftp: scheme or an embedded '://' is rejected)."
        )

    async def load_stored(self, path: str, *, with_mime: bool = True) -> tuple[bytes, str | None]:
        """Read the stored object at storage id ``path`` as ``(bytes, mime)``.

        ``path`` is always a storage id, never a URL, whatever characters it holds.
        A storage id is a caller-supplied logical key, so root containment is
        enforced before it reaches the provider (an ``UnsafeTemplatePathError``
        propagates); every storage read, including :meth:`load`'s storage branch,
        is guarded at this one seam. ``with_mime`` is as in :meth:`load`.
        """
        safe_template_path(path)
        if not with_mime:
            return await self._storage().load_bytes(path), None
        stored = await self._storage().load_object(path)
        return stored.data, stored.content_type

    async def load_file(self, source: str) -> str | MediaBlock:
        """Load ``source`` and decode it to extracted text OR a ``MediaBlock``.

        Resolves the bytes as :meth:`load` does, the type being re-detected here with
        Magika so no storage mime is read; a storage id is read bounded by the
        ``FILE_LOADING_MAX_BYTES`` cap, so an object over it is refused without being
        read whole. The bytes are then dispatched through the type registry (the opt-in
        ``files`` extra). Bytes of an unknown type, over the size cap, detected as text
        but not UTF-8, or refused by their extractor raise
        :class:`ContentNotLoadableError`. Without the ``files`` extra it raises the
        ``501`` ``NotSupportedError`` before any read.
        """
        from tai42_skeleton.template.file_loading import load_content, read_within_cap

        if _names_storage_id(source):
            safe_template_path(source)
            data = await read_within_cap(self._storage(), source)
        else:
            data, _mime = await self.load(source, with_mime=False)
        return await asyncio.to_thread(load_content, data, source)

    async def resolve_image(self, source: str | bytes) -> ImageReference:
        """Resolve an image reference to an addressable image with its asserted mime.

        Image-only: the resolved mime must be ``image/*`` or it raises. Per source
        kind, without a wasteful download:

        - a public ``http(s)`` URL is passed through unchanged (the model
          dereferences it) with its suffix mime; a non-``image/*`` URL suffix raises
          here, an indeterminable one passes through with no mime.
        - a ``data:`` URI's mediatype is asserted ``image/*`` and passed through.
        - a storage ``id`` (or raw ``bytes``) is loaded and emitted as a base64
          ``data:`` URI that always carries a resolved image mime.
        """
        if isinstance(source, bytes):
            from tai42_skeleton.template.file_loading import detect_mime

            mime = detect_mime(source)
            assert_image(mime, "<bytes>")
            if mime is None:
                raise AssertionError
            return ImageReference(url=data_uri(source, mime), mime=mime)

        parts = urlsplit(source)
        scheme = parts.scheme
        if scheme in ("http", "https"):
            mime, _ = mimetypes.guess_type(parts.path)
            if mime is not None and not mime.startswith("image/"):
                raise ValueError(
                    f"resolve_image requires an image resource; URL suffix mime is {mime!r} for {source!r}"
                )
            return ImageReference(url=source, mime=mime)
        if scheme == "data":
            mediatype = source[len("data:") :].partition(",")[0]
            mime = mediatype.split(";", 1)[0] or None
            assert_image(mime, source)
            return ImageReference(url=source, mime=mime)

        # Storage id (empty scheme) or a bad scheme -> load() validates and raises.
        data, mime = await self.load(source)
        assert_image(mime, source)
        if mime is None:
            raise AssertionError
        return ImageReference(url=data_uri(data, mime), mime=mime)
