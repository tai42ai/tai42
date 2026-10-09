"""The turn text and entry-params a channel bridges for one inbound-media message.

A channel that receives media a participant sent bridges it as a normal turn — the rendered
``text`` plus the opaque ``media_*`` entry-params shaped here — never by fetching bytes and never
by minting typed ``attachments``. The platform attaches NO meaning and NO trust to the values: a
tool consumer opts into the keys it understands, the same way it reads any other enrichment
``params``. The wire vocabulary (:class:`~tai42_contract.conversations.InboundMediaKind` and the
``MEDIA_*_PARAM`` keys) is contract vocabulary.

Each value is bounded per value by :func:`~tai42_kit.conversations.entry_params.put_entry_param`
(an over-cap value is dropped and counted, never truncated); the aggregate ``params`` bound is
enforced separately at accept time.
"""

from __future__ import annotations

from tai42_contract.conversations import (
    MEDIA_FILENAME_PARAM,
    MEDIA_ID_PARAM,
    MEDIA_KIND_PARAM,
    MEDIA_MIME_TYPE_PARAM,
    MEDIA_SHA256_PARAM,
    MEDIA_SIZE_PARAM,
    MEDIA_VOICE_PARAM,
    STICKER_ANIMATED_PARAM,
    InboundMediaKind,
)

from tai42_kit.conversations.entry_params import put_entry_param

__all__ = ["build_inbound_media_params", "inbound_media_placeholder"]


def build_inbound_media_params(
    *,
    kind: InboundMediaKind | str,
    media_id: str | None = None,
    mime_type: str | None = None,
    filename: str | None = None,
    sha256: str | None = None,
    voice: bool = False,
    animated: bool = False,
    size: int | None = None,
    door: str,
) -> dict[str, str]:
    """The opaque ``media_*`` entry-params for one bridged inbound-media turn.

    Pure shaping, no I/O. ``kind`` is coerced through :class:`~tai42_contract.conversations.InboundMediaKind`
    so an unknown wire kind raises ``ValueError`` loudly (a channel that cannot map its vendor kind
    calls ``notify_inbound_rejected`` instead of bridging). ``door`` names the channel in the drop
    log and counter.
    """
    resolved = InboundMediaKind(kind)
    params: dict[str, str] = {}
    put_entry_param(params, MEDIA_KIND_PARAM, resolved.value, door=door)
    put_entry_param(params, MEDIA_ID_PARAM, media_id, door=door)
    put_entry_param(params, MEDIA_MIME_TYPE_PARAM, mime_type, door=door)
    put_entry_param(params, MEDIA_SHA256_PARAM, sha256, door=door)
    put_entry_param(params, MEDIA_FILENAME_PARAM, filename, door=door)
    if voice:
        put_entry_param(params, MEDIA_VOICE_PARAM, "true", door=door)
    if animated:
        put_entry_param(params, STICKER_ANIMATED_PARAM, "true", door=door)
    if size is not None:
        put_entry_param(params, MEDIA_SIZE_PARAM, str(size), door=door)
    return params


def inbound_media_placeholder(kind: InboundMediaKind | str, *, filename: str | None = None, voice: bool = False) -> str:
    """The non-blank turn text for a caption-less inbound-media message.

    ``accept`` refuses blank text, so this guarantees the message is never lost. A voice audio
    reads ``[voice message]``; a document/file with a filename reads ``[document: <name>]`` /
    ``[file: <name>]``; otherwise the bracketed kind. Exhaustive over
    :class:`~tai42_contract.conversations.InboundMediaKind` (an unknown kind raises ``ValueError`` —
    never a silent ``[media]`` fallback).
    """
    resolved = InboundMediaKind(kind)
    if resolved is InboundMediaKind.AUDIO and voice:
        return "[voice message]"
    if resolved in (InboundMediaKind.DOCUMENT, InboundMediaKind.FILE):
        name = filename.strip() if isinstance(filename, str) and filename.strip() else None
        if name:
            return f"[{resolved.value}: {name}]"
    return f"[{resolved.value}]"
