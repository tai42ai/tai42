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
  context_message_id  — a reply-to's ``context.id`` (the quoted/replied-to ``wamid``),
                        on any message that quotes an earlier one.
  referral_source_url — a click-to-WhatsApp / QR ``referral.source_url``.
  referral_source_id  — the ad/post ``referral.source_id``.
  referral_source_type— the ``referral.source_type`` (e.g. ``ad``/``post``).
  referral_ctwa_clid  — the click-to-WhatsApp click id ``referral.ctwa_clid``.
  referral_headline   — the referral's ``headline`` when present.
  referral_body       — the referral's ``body`` when present.
  media_kind          — an inbound media message's wire type
                        (``image``/``document``/``audio``/``video``/``sticker``).
  media_id            — the Graph media id of an inbound media object; a consumer with
                        operator credentials fetches the bytes off the Graph media endpoint
                        (see the INBOUND MEDIA design note in ``rich_content`` — the channel
                        does not re-host the bytes, so the file is reached through this id).
  media_mime_type     — the media object's ``mime_type``.
  media_sha256        — the media object's ``sha256`` (content integrity).
  media_filename      — an inbound document's ``filename`` (document only).
  media_voice         — ``"true"`` when an inbound audio is a voice note (``audio.voice``).
  sticker_animated    — ``"true"`` for an animated sticker (``sticker.animated``).
  reaction_emoji      — a reaction message's ``reaction.emoji`` (absent = a REMOVED reaction).
  reaction_message_id — the ``wamid`` a reaction was applied to (``reaction.message_id``).
  contacts_count      — the number of contact cards in a ``contacts`` message.
  contacts            — the raw ``contacts`` array as a compact JSON string (dropped when it
                        exceeds the per-value transport cap; ``contacts_count`` still rides).

All values are transport-bounded by the contract (:func:`validate_entry_params`); an
individual value over ``ENTRY_PARAM_VALUE_MAX_CHARS`` is dropped at extraction and, in
the rare event the aggregate still overflows a bound, the whole params set is dropped
and the turn bridges without it — a participant message is never lost to a params bound.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.conversations import ENTRY_PARAM_VALUE_MAX_CHARS

logger = logging.getLogger(__name__)

_REFERRAL_PARAM_KEYS: dict[str, str] = {
    "source_url": "referral_source_url",
    "source_id": "referral_source_id",
    "source_type": "referral_source_type",
    "ctwa_clid": "referral_ctwa_clid",
    "headline": "referral_headline",
    "body": "referral_body",
}


def _message_context_params(message: dict[str, Any]) -> dict[str, str]:
    """The opaque entry-params a bridged turn carries from a message's ``referral`` and reply-to ``context``.

    ``referral`` is a click-to-WhatsApp / QR entry. Forwarded verbatim as strings,
    no interpretation. A missing/non-string/empty field is skipped, and a value over
    the contract's per-value cap is dropped (the transport bound is enforced
    end-to-end by :func:`~tai42_contract.conversations.validate_entry_params`); the
    key vocabulary is the module's ``_REFERRAL_PARAM_KEYS`` plus
    ``context_message_id``.
    """
    params: dict[str, str] = {}
    referral = message.get("referral")
    if isinstance(referral, dict):
        for field, key in _REFERRAL_PARAM_KEYS.items():
            _put_param(params, key, referral.get(field))
    context = message.get("context")
    if isinstance(context, dict):
        _put_param(params, "context_message_id", context.get("id"))
    return params


def _put_param(params: dict[str, str], key: str, value: Any) -> None:
    """Add ``key`` iff ``value`` is a non-empty string within the contract's per-value cap.

    An over-cap opaque value is dropped (never truncated — truncation would
    silently corrupt an opaque token); a debug line records the drop without ever
    logging the value.
    """
    if not isinstance(value, str) or not value:
        return
    if len(value) > ENTRY_PARAM_VALUE_MAX_CHARS:
        logger.debug("dropping whatsapp inbound param %r: value over the %d-char cap", key, ENTRY_PARAM_VALUE_MAX_CHARS)
        return
    params[key] = value


def _merged_params(base: dict[str, str], extra: dict[str, str]) -> dict[str, str] | None:
    """``base`` merged with ``extra`` (both already per-value bounded), or ``None`` when empty.

    ``base`` is never mutated. ``extra`` wins on a key collision, though the
    channel's key spaces do not overlap by construction.
    """
    if not base and not extra:
        return None
    merged = {**base, **extra}
    return merged or None
