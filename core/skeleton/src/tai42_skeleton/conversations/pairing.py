"""Pure intercept classification for a pairing turn.

One inbound message is classified — with NO model judgement and NO fuzzy matching — into
one of four actions the turn engine dispatches on: mint a fresh pair code (``/link``),
detach the sending address (``/unlink``), redeem a carried pair code, or pass the message
through untouched to the target agent/tool.

The two commands match ONLY as the whole trimmed message (``/link``/``/unlink`` — never
``/link extra``, which is ordinary text). A code redemption matches the first
``LINK-XXXXXXXX`` token anywhere in the raw text, so a code carried inside a sentence, or
pasted from an invite link, still redeems; multiple codes → the first wins. Anything else is
a passthrough. On a target with multichannel OFF the caller never routes here, so all four
forms reach the target as plain text (byte-identical to today)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# The D1 pair-code shape (``LINK-`` + 8 ``[A-Z0-9]``), matched as a whole token so a longer
# alphanumeric run adjacent to a valid-looking prefix is not mistaken for a code.
_CODE_RE = re.compile(r"\bLINK-[A-Z0-9]{8}\b")


@dataclass(frozen=True)
class Link:
    """The whole trimmed message was ``/link`` — mint a fresh pair code for this conversation."""


@dataclass(frozen=True)
class Unlink:
    """The whole trimmed message was ``/unlink`` — detach the sending address from its person."""


@dataclass(frozen=True)
class Redeem:
    """The message carries a pair code (the first match) to redeem into a merge."""

    code: str


@dataclass(frozen=True)
class Passthrough:
    """Ordinary text — routed unchanged to the target agent/tool."""


#: An inbound message classifies to exactly one of these four actions.
PairingAction = Link | Unlink | Redeem | Passthrough


def classify(text: str) -> PairingAction:
    """Classify ``text`` into a :data:`PairingAction`. Exact-match commands first (the whole
    trimmed message), then the first embedded pair code, else a passthrough."""
    trimmed = text.strip()
    if trimmed == "/link":
        return Link()
    if trimmed == "/unlink":
        return Unlink()
    match = _CODE_RE.search(text)
    if match is not None:
        return Redeem(match.group(0))
    return Passthrough()


__all__ = ["Link", "PairingAction", "Passthrough", "Redeem", "Unlink", "classify"]
