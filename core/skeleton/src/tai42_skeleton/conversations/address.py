"""Canonical address forms for route matching and thread keying.

Route matching is EXACT equality on the canonical form of both sides, and canonicalization
is deliberately channel-AGNOSTIC: trim surrounding whitespace, reject a blank, compare
verbatim. Medium-specific normalization (E.164, a lower-cased handle) belongs to the
channel adapter, which must present the same form at route create and at inbound accept.
The API door has no adapter, so its caller owns presenting one end user under one spelling
— two spellings are two threads and two rate buckets.
"""

from __future__ import annotations


def canonical_address(value: str) -> str:
    """Return the canonical form of a channel address — surrounding whitespace trimmed.

    Raises ``ValueError`` on a value blank once trimmed, which would key no route
    and collide with every other blank.
    """
    if not isinstance(value, str):
        raise ValueError("address must be a string")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("address must be non-blank")
    return trimmed


__all__ = ["canonical_address"]
