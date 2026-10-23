"""The one type for a participant-facing reply text and its validator.

A conversation route's ``reply_texts`` members, the ``reply`` a target's terminal failure carries
(:class:`~tai42_contract.interactions.RunTerminalFailed`, :class:`~tai42_contract.interactions.RunFailed`)
and a caller-set refusal sentence share one bound: a single participant-facing message, so a
small cap (channels impose their own tighter message caps), non-blank so a set value never
delivers an empty message.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field, Strict, TypeAdapter

#: A non-blank text: at least one non-whitespace character.
NON_BLANK_PATTERN: Final = r"\S"

#: Cap on a single participant-facing reply text.
REPLY_TEXT_MAX_CHARS: Final = 2000

#: A participant-facing reply text: a non-blank string of at most :data:`REPLY_TEXT_MAX_CHARS`
#: characters. Strict: a non-string value is refused, never coerced; the text is never trimmed.
ReplyText = Annotated[str, Strict(), Field(min_length=1, max_length=REPLY_TEXT_MAX_CHARS, pattern=NON_BLANK_PATTERN)]

_REPLY_TEXT_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(ReplyText)


def validate_reply_text(value: object) -> str:
    """Return ``value`` when it is a valid :data:`ReplyText`; raise :class:`pydantic.ValidationError` otherwise."""
    return _REPLY_TEXT_ADAPTER.validate_python(value)
