"""Inbound entry-params vocabulary — the opaque strings a bridged turn carries.

The channel's PUBLIC contract for the opaque ``payload["params"]`` a
channel-agnostic tool consumer reads. Every key below is forwarded VERBATIM as a
string and carries NO platform interpretation; a consumer opts into whichever keys
it understands. Params ride ONLY on the conversation-bridge path (a fresh turn via
``conversations.accept``); the correlated-answer path forwards ``{"answer": …}`` to
the callback door, a seam that carries no params, so a tap/button that ANSWERS a
pending question does not surface these (its id is already consumed to select the
option). Keys, and where each is set:

  reply_id            — an interactive tap's ``button_reply.id`` / ``list_reply.id``
                        (the question-bound wire id), bridged when the tap is NOT an
                        answer (no/other pending ask).
  reply_description   — a ``list_reply.description`` when the picked row carried one.
  button_payload      — a template quick-reply tap's ``button.payload`` (the developer-
                        defined payload behind the visible ``button.text``).
  referral_source_url — a click-to-WhatsApp / QR ``referral.source_url``.
  referral_source_id  — the ad/post ``referral.source_id``.
  referral_source_type— the ``referral.source_type`` (e.g. ``ad``/``post``).
  referral_ctwa_clid  — the click-to-WhatsApp click id ``referral.ctwa_clid``.
  referral_headline   — the referral's ``headline`` when present.
  referral_body       — the referral's ``body`` when present.
  media_kind          — an inbound media message's wire type
                        (``image``/``document``/``audio``/``video``/``sticker``).
  media_id            — the SERVED media id an inbound media object was ingested to; a consumer
                        reads the bytes off the platform's served-media route by this id (the
                        channel fetches, ingests, and re-hosts the bytes — see the INBOUND MEDIA
                        note in ``rich_content``). Rides beside the typed ``attachments`` entry.
  media_mime_type     — the ingested media's sniffed-and-agreed mime type.
  media_sha256        — the ingested media's ``sha256`` (content integrity).
  media_size          — the ingested media's actual byte size.
  media_filename      — an inbound document's SANITISED filename (document only).
  media_voice         — ``"true"`` when an inbound audio is a voice note (``audio.voice``).
  sticker_animated    — ``"true"`` for an animated sticker (``sticker.animated``).
  reaction_emoji      — a reaction message's ``reaction.emoji`` (absent = a REMOVED reaction).
  reaction_message_id — the ``wamid`` a reaction was applied to (``reaction.message_id``).
  contacts_count      — the number of contact cards in a ``contacts`` message.
  contacts            — the raw ``contacts`` array as a compact JSON string (dropped when it
                        exceeds the per-value transport cap; ``contacts_count`` still rides).

All values are transport-bounded by the contract through the kit's entry-params policy
(``tai42_kit.conversations.entry_params``): an individual value over
``ENTRY_PARAM_VALUE_MAX_CHARS`` is dropped at extraction (:func:`put_entry_param`, logged and
counted) and, in the rare event the aggregate still overflows a bound, the whole params set is
dropped and the turn bridges without it — a participant message is never lost to a params bound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tai42_kit.conversations.entry_params import put_entry_param

logger = logging.getLogger(__name__)

_REFERRAL_PARAM_KEYS: dict[str, str] = {
    "source_url": "referral_source_url",
    "source_id": "referral_source_id",
    "source_type": "referral_source_type",
    "ctwa_clid": "referral_ctwa_clid",
    "headline": "referral_headline",
    "body": "referral_body",
}


@dataclass(frozen=True)
class _MessageContext:
    """What one inbound message carries to whichever turn or answer it becomes.

    ``params`` are its message-level entry-params (the referral keys); ``reply_to_medium_message_id``
    is the ``wamid`` of the message it quotes (its ``context.id``), ``None`` when it quotes none.
    """

    params: dict[str, str]
    reply_to_medium_message_id: str | None


def _message_context_params(message: dict[str, Any]) -> dict[str, str]:
    """The opaque entry-params a bridged turn carries from a message's ``referral``.

    ``referral`` is a click-to-WhatsApp / QR entry. Forwarded verbatim as strings,
    no interpretation. A missing/non-string/empty field is skipped, and a value over
    the contract's per-value cap is dropped (logged and counted); the key vocabulary is the
    module's ``_REFERRAL_PARAM_KEYS``.
    """
    params: dict[str, str] = {}
    referral = message.get("referral")
    if isinstance(referral, dict):
        for field, key in _REFERRAL_PARAM_KEYS.items():
            put_entry_param(params, key, referral.get(field), door="whatsapp")
    return params


def _reply_to_message_id(message: dict[str, Any]) -> str | None:
    """The ``wamid`` of the message ``message`` quotes: its ``context.id`` when that is a non-blank string."""
    context = message.get("context")
    if not isinstance(context, dict):
        return None
    quoted = context.get("id")
    if isinstance(quoted, str) and quoted.strip():
        return quoted
    return None


def _message_context(message: dict[str, Any]) -> _MessageContext:
    """The entry-params and the quoted ``wamid`` of one inbound message."""
    return _MessageContext(
        params=_message_context_params(message), reply_to_medium_message_id=_reply_to_message_id(message)
    )


def _merged_params(base: dict[str, str], extra: dict[str, str]) -> dict[str, str] | None:
    """``base`` merged with ``extra`` (both already per-value bounded), or ``None`` when empty.

    ``base`` is never mutated. ``extra`` wins on a key collision, though the
    channel's key spaces do not overlap by construction.
    """
    if not base and not extra:
        return None
    merged = {**base, **extra}
    return merged or None
