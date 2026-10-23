"""The per-identity words store: one record per web route, beside the route's entry gate."""

from __future__ import annotations

import json

from pydantic import ValidationError
from tai42_kit.clients.impl.redis import scan_iter

from tai42_channel_web.store.connection import _as_str, _redis
from tai42_channel_web.words import WebWords, stored_payload

_WORDS_PREFIX = "channel:web:words:"


class WordsRecordError(Exception):
    """A stored words record does not parse as :class:`~tai42_channel_web.words.WebWords`.

    Only the words doors and the backup importer write the record, both through the model, so a
    record that fails it was written by something else; it is refused loudly, never drawn as
    the built-in words.
    """


def _words_key(identity: str) -> str:
    return f"{_WORDS_PREFIX}{identity}"


def _decode(identity: str, raw: str | bytes) -> WebWords:
    try:
        return WebWords.model_validate_json(raw)
    except ValidationError as exc:
        raise WordsRecordError(f"the stored words for web route {identity!r} are not valid words: {exc}") from exc


async def get_words(identity: str) -> WebWords | None:
    """The words stored for ``identity``, or ``None`` when none are set."""
    async with _redis() as redis:
        raw = await redis.get(_words_key(identity))
    return None if raw is None else _decode(identity, raw)


async def set_words(identity: str, words: WebWords) -> None:
    """Store ``words`` as ``identity``'s whole words object, replacing any already set."""
    async with _redis() as redis:
        await redis.set(_words_key(identity), json.dumps(stored_payload(words)))


async def clear_words(identity: str) -> bool:
    """Delete ``identity``'s words; ``True`` when some were set."""
    async with _redis() as redis:
        return await redis.delete(_words_key(identity)) > 0


async def list_words() -> dict[str, WebWords]:
    """Every web route's stored words, keyed by identity.

    SCAN over the words prefix, which carries no identity and so no glob metacharacter; each
    key's identity is read back verbatim after the prefix. A record deleted between the SCAN and
    the GET is skipped.
    """
    listed: dict[str, WebWords] = {}
    async with _redis() as redis:
        async for key in scan_iter(redis, f"{_WORDS_PREFIX}*"):
            key_str = _as_str(key)
            raw = await redis.get(key_str)
            if raw is None:
                continue
            identity = key_str[len(_WORDS_PREFIX) :]
            listed[identity] = _decode(identity, raw)
    return listed
