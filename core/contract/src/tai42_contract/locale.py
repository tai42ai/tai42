"""The one canonical form and validator for a subject's locale — a BCP 47 language
tag the platform's rendering layer resolves text against.

Kept dependency-free (no ``babel`` in the contract layer) so every wire model that
carries a locale — a :class:`~tai42_contract.conversations.Person`, the ambient
:class:`~tai42_contract.states.SubjectCandidates` — validates and canonicalizes it
through this ONE seam. The canonical form lowercases the language subtag, uppercases a
two-letter region and titlecases a four-letter script, so ``He-il`` and ``he-IL`` store
identically and the render layer's fallback chain is built from a single spelling.

A locale is ALWAYS explicit: ``None`` means the door supplied none (never a silent
default to any language); a present value must be a well-formed tag or the boundary
rejects it loudly.
"""

from __future__ import annotations

import re

# A pragmatic BCP 47 subset: a 2-3 letter primary language subtag, then any number of
# 2-8 alnum subtags (script, region, variant). It admits every tag the platform ships
# and rejects free text, an empty subtag (a stray ``-``) or an overrun subtag.
_LANGTAG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


class InvalidLocaleError(ValueError):
    """A supplied locale is not a well-formed BCP 47 language tag."""


def canonical_locale(tag: str) -> str:
    """Return the canonical BCP 47 spelling of ``tag`` (language lowercase, a 2-letter
    region uppercase, a 4-letter script titlecase), or raise
    :class:`InvalidLocaleError` when it is not a well-formed tag.

    A blank or malformed tag raises — the boundary never stores an unusable locale."""
    stripped = tag.strip()
    if not _LANGTAG_RE.match(stripped):
        raise InvalidLocaleError(f"{tag!r} is not a well-formed BCP 47 language tag")
    parts = stripped.split("-")
    out = [parts[0].lower()]
    for sub in parts[1:]:
        if len(sub) == 2 and sub.isalpha():
            out.append(sub.upper())
        elif len(sub) == 4 and sub.isalpha():
            out.append(sub.title())
        else:
            out.append(sub.lower())
    return "-".join(out)


def normalize_optional_locale(tag: str | None) -> str | None:
    """Canonicalize ``tag`` when present; pass ``None`` through unchanged — the explicit
    "no locale supplied" marker every carrier stores rather than a silent default."""
    if tag is None:
        return None
    return canonical_locale(tag)
