"""Form pictures: a picture given as a link or a stored reference becomes picture data a WhatsApp Flow can draw.

A Flow ``Image`` draws base64 picture data only. An image block's ``src`` (or a reaction's
``display`` value for an image slot) is one of three things. An ``http(s)`` link is fetched through
the kit's guarded :func:`~tai42_kit.net.fetch_media` — the platform URL guard and its settings
apply. A same-origin stored-media reference (``/api/interactions/media/<id>``) is read in process
through the platform's media facet. Either picture is checked to be a JPEG or PNG within the
picture size and embedded as base64. Any other ``src`` is taken as picture data already and passed
on unchanged. A link that cannot be parsed, or a picture that cannot be fetched or read, is too
large or is in another format, refuses the send loudly; nothing is drawn in its place. A fetch that
does not end within the module's total time limit is a retryable delivery failure. The stored id is
a capability secret: no refusal text names it.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError
from tai42_contract.interactions import (
    MediaNotFoundError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    served_media_id,
)
from tai42_kit.net import MediaFetchError, UrlGuardError, fetch_media, media_url_host
from tai42_kit.net.request_body import RequestBodyTooLargeError

from tai42_channel_whatsapp.flows_endpoint import echoed_screen_data_max_bytes
from tai42_channel_whatsapp.flows_resolve import image_slots

# Meta's Flow Image reference: "Recommended image size: Up to 300kb". The figure is a
# recommendation, not a stated refusal boundary; it is enforced as the cap.
_PICTURE_MAX_BYTES = 300_000
# Seconds one picture fetch may take in all: resolving the host, connecting, every redirect hop and
# the whole body. A reaction's picture is fetched while WhatsApp waits for the data endpoint's
# answer, which the vendor documents as 10 seconds, so this stays well under it.
_PICTURE_FETCH_SECONDS = 5
# Meta's Flow Image reference: "Total data channel payload size: 1 Mb". Read as the whole
# data of a send or an answer that carries slotted pictures; the figure is documented, and
# what the vendor does above it is not.
_PICTURED_DATA_MAX_BYTES = 1_000_000

# The leading bytes of the formats a Flow Image draws. The response ``Content-Type`` is not used.
_PICTURE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")

_LINK_SCHEMES = ("http", "https")
# What the URL standard, and ``urlsplit`` with it, drops before reading a scheme: the C0 controls
# and the space leading the text, and every tab and newline.
_LEADING_IGNORED = "".join(chr(code) for code in range(0x21))
_IGNORED_ANYWHERE = str.maketrans("", "", "\t\r\n")


def is_picture_link(src: Any) -> bool:
    """Whether ``src`` is an ``http`` or ``https`` link (the scheme in any case).

    Only the scheme decides, read as ``urlsplit`` reads it: the text before the first ``:``. The
    rest is not parsed here, so a link whose rest cannot be parsed is still a link (refused before
    anything is fetched) and text with no such scheme, base64 picture data included, is never misjudged.
    """
    if not isinstance(src, str):
        return False
    scheme, colon, _ = src.lstrip(_LEADING_IGNORED).translate(_IGNORED_ANYWHERE).partition(":")
    return bool(colon) and scheme.lower() in _LINK_SCHEMES


async def embed_page_pictures(pages: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """A copy of ``pages`` with every image block's picture ``src`` replaced by its base64 picture data.

    A picture ``src`` is a link or a stored reference; other blocks and other ``src`` values are
    kept as they are; ``None`` stays ``None``. The distinct pictures of the form are fetched or
    read concurrently, each once however many blocks name it. Raises ``ChannelInputError`` for a
    link that cannot be parsed (before anything is fetched or read), for a link the URL guard
    refuses, for a fetch fault the kit reports as permanent, and for a missing, oversized or
    undrawable picture; ``ChannelDeliveryError`` for a fetch fault the kit reports as transient, a
    fetch over the time limit, or an unavailable media store; each names the page and picture. A
    fault of the media store itself propagates unchanged.
    """
    embedded = copy.deepcopy(pages)
    blocks: list[dict[str, Any]] = []
    places: dict[str, str] = {}
    for page_number, page in enumerate(embedded or [], start=1):
        images = [
            block for block in page.get("display") or [] if isinstance(block, dict) and block.get("kind") == "image"
        ]
        for picture_number, block in enumerate(images, start=1):
            if _is_picture_source(block.get("src")):
                blocks.append(block)
                places.setdefault(block["src"], f"page {page_number}, picture {picture_number}")
    pictures = await _resolve_pictures(places)
    for block in blocks:
        block["src"] = pictures[block["src"]]
    return embedded


async def embed_display_pictures(
    display: dict[str, Any] | None, pages: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """A copy of a reaction's ``display`` update with each image slot's picture replaced by base64 picture data.

    Only a value whose slot is an image block on ``pages`` and which is a link or a stored
    reference is fetched or read; a text slot's value is kept as text whatever it looks like.
    ``None`` stays ``None``. Raises as :func:`embed_page_pictures` does, naming the slot.
    """
    if display is None:
        return None
    pictured = image_slots(pages)
    sourced = {slot: value for slot, value in display.items() if slot in pictured and _is_picture_source(value)}
    places: dict[str, str] = {}
    for slot, value in sourced.items():
        places.setdefault(value, f"display slot {slot!r}")
    pictures = await _resolve_pictures(places)
    return {slot: pictures[value] if slot in sourced else value for slot, value in display.items()}


def require_pictured_data_fits(pages: list[dict[str, Any]] | None, data: dict[str, Any], *, endpoint: bool) -> None:
    """Refuse screen ``data`` too large for a form with picture slots; a form without one is not measured.

    Slotted pictures ride the screen's data. The size is the compact JSON's UTF-8 length. The
    budget is Meta's figure for data that carries pictures, and for an endpoint-driven form
    (``endpoint``) no more than the data whose footer request the data endpoint can read
    (:func:`~tai42_channel_whatsapp.flows_endpoint.echoed_screen_data_max_bytes`). Raises
    ``ChannelInputError`` over the budget.
    """
    if not image_slots(pages):
        return
    budget = min(_PICTURED_DATA_MAX_BYTES, echoed_screen_data_max_bytes()) if endpoint else _PICTURED_DATA_MAX_BYTES
    size = len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > budget:
        raise ChannelInputError(
            f"form data is {size} bytes, over the {budget} bytes this channel sends in a form with picture "
            "slots; slotted pictures ride this data and count toward it"
        )


def _stored_id(src: Any) -> str | None:
    # The id of a same-origin stored-media reference, or ``None``. Asked after
    # ``is_picture_link``, so an absolute served reference is a link and is fetched.
    return served_media_id(src) if isinstance(src, str) else None


def _is_picture_source(src: Any) -> bool:
    return is_picture_link(src) or _stored_id(src) is not None


async def _resolve_pictures(places: dict[str, str]) -> dict[str, str]:
    # Resolve every picture source of ``places`` (source -> where it sits) at once; the base64
    # data per source. Every link is parsed first, so one that cannot be parsed refuses the form
    # before any fetch or read. Every fetch and read is awaited to its end; the first failure in
    # form order is raised.
    sources = list(places)
    hosts = {src: _link_host(src, places[src]) for src in sources if is_picture_link(src)}
    stored_ids = {src: media_id for src in sources if src not in hosts and (media_id := _stored_id(src)) is not None}
    results = await asyncio.gather(
        *(
            _link_picture(src, places[src], hosts[src])
            if src in hosts
            else _stored_picture(stored_ids[src], places[src])
            for src in sources
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return dict(zip(sources, (str(result) for result in results), strict=True))


def _link_host(url: str, where: str) -> str:
    # The link's host for the refusal texts, read by the fetch's own parser; a link that parser
    # refuses is refused here.
    try:
        return media_url_host(url)
    except MediaFetchError as exc:
        raise ChannelInputError(f"form picture link cannot be parsed ({where})") from exc


def _drawable(body: bytes, where: str, source: str) -> str:
    # The base64 of a drawable picture; ``source`` names where its bytes came from. The leading
    # bytes decide the format; a response ``Content-Type`` or a stored mime is not used.
    if not body.startswith(_PICTURE_SIGNATURES):
        raise ChannelInputError(f"form picture is neither JPEG nor PNG, the formats WhatsApp draws ({where}, {source})")
    return base64.b64encode(body).decode("ascii")


async def _link_picture(url: str, where: str, host: str) -> str:
    try:
        async with asyncio.timeout(_PICTURE_FETCH_SECONDS):
            body, _ = await fetch_media(url, follow_redirects=True, max_bytes=_PICTURE_MAX_BYTES)
    except TimeoutError as exc:
        raise ChannelDeliveryError(
            f"form picture was not fetched within {_PICTURE_FETCH_SECONDS} seconds ({where}, host {host!r})",
            retryable=True,
        ) from exc
    except UrlGuardError as exc:
        raise ChannelInputError(f"form picture link refused ({where}): {exc}") from exc
    except MediaFetchError as exc:
        message = f"form picture could not be fetched ({where}): {exc}"
        if exc.transient:
            raise ChannelDeliveryError(message, retryable=True) from exc
        raise ChannelInputError(message) from exc
    except RequestBodyTooLargeError as exc:
        raise ChannelInputError(_too_large(where, f"host {host!r}")) from exc
    return _drawable(body, where, f"host {host!r}")


async def _stored_picture(media_id: str, where: str) -> str:
    try:
        stored = await tai42_app.media.read_media(media_id, max_bytes=_PICTURE_MAX_BYTES)
    except MediaNotFoundError as exc:
        raise ChannelInputError(
            f"stored form picture was not found ({where}): it is unknown, has expired, "
            "or is not attached to a message yet"
        ) from exc
    except MediaStoreUnavailableError as exc:
        raise ChannelDeliveryError(
            f"stored form picture could not be read ({where}): the media store is unavailable"
        ) from exc
    except MediaTooLargeError as exc:
        raise ChannelInputError(_too_large(where, "stored picture")) from exc
    return _drawable(stored.data, where, "stored picture")


def _too_large(where: str, source: str) -> str:
    return (
        f"form picture is larger than the {_PICTURE_MAX_BYTES} bytes this channel embeds in a form ({where}, {source})"
    )
