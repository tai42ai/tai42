"""The keyset page-cursor codec and a subject's four addressing columns."""

from __future__ import annotations

from tai42_contract.states.errors import ValueValidationError
from tai42_contract.states.models import StateSubject


def _subject_cols(subject: StateSubject) -> tuple[str, str, str, str]:
    """A subject's four addressing columns ``(target_kind, target_name, kind, key)``."""
    return (subject.target_kind, subject.target_name, subject.kind, subject.key)


def _split_cursor(cursor: str) -> tuple[str, str, str, str]:
    r"""Unpack a ``(target_kind, target_name, subject_kind, subject_key)`` keyset cursor.

    Packed as ``"<tk>\\x00<tn>\\x00<kind>\\x00<key>"`` — the FULL subject identity, so no two rows
    sharing a ``(subject_kind, subject_key)`` across targets collide at a page boundary. A
    client-supplied cursor that does not carry the four packed parts is a bad input (422), never a
    500 from unpacking deep in the query.
    """
    parts = cursor.split("\x00")
    if len(parts) != 4:
        raise ValueValidationError("cursor is malformed; use only a cursor returned by a prior page")
    tk, tn, kind, key = parts
    return tk, tn, kind, key


def make_cursor(target_kind: str, target_name: str, kind: str, key: str) -> str:
    """Pack a ``(target_kind, target_name, subject_kind, subject_key)`` keyset cursor."""
    return f"{target_kind}\x00{target_name}\x00{kind}\x00{key}"
