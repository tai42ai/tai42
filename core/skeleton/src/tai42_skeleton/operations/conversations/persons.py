"""Linked-person read doors.

Fetch a person row (identity, folded addresses, stored locale) and set or clear its
operator-declared locale override.
"""

from __future__ import annotations

import sys
from typing import Any

from tai42_contract.conversations import Person
from tai42_contract.locale import InvalidLocaleError, normalize_optional_locale

from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.errors import NotSupportedError

from .backend import _require_backend

# The ``operations.conversations`` package instance THIS submodule belongs to, captured from
# ``sys.modules`` at import. A registry reload pops and re-imports the whole package, so each
# generation's submodules bind to their OWN package object here — a stale-but-orphaned handler
# then still reads (and a test still patches) the same generation it was built with. This is the
# package-alias test-double seam for ``get_conversations_manager``/``resolve_caller``/
# ``assert_execution_key_bindable``/``_person_store``.
_pkg = sys.modules["tai42_skeleton.operations.conversations"]


@operation(
    summary="Read a conversation person",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=Person,
)
async def get_conversation_person(person_id: str) -> dict[str, Any]:
    """The person row named by ``person_id`` — its identity, folded addresses and stored ``locale``.

    ``locale`` is the BCP 47 tag the rendering layer resolves text against, or ``null`` when
    none is known. The subject read that serves a person's locale. A blank ``person_id`` is a
    400; an unknown one a 404; no backend a loud 501.
    """
    if not person_id.strip():
        raise BadRequestError("person_id must be a non-blank person identifier")
    _require_backend()
    person = await _pkg._person_store().get_by_id(person_id)
    if person is None:
        raise NotFoundError(f"conversation person not found: {person_id!r}")
    return person.model_dump(mode="json")


@operation(
    summary="Set a conversation person's locale",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=Person,
)
async def set_conversation_person_locale(person_id: str, locale: str | None) -> dict[str, Any]:
    """Set (or clear) a person's stored ``locale``.

    The operator override the rendering layer resolves text against, winning over the
    channel-seeded value on every later turn. ``locale`` is a BCP 47 tag (canonicalized here —
    ``he-il`` stores as ``he-IL``); ``null`` clears it back to no-locale-known. A blank
    ``person_id`` or a malformed ``locale`` is a 400; an unknown person a 404; no backend a loud
    501. Returns the updated person.
    """
    if not person_id.strip():
        raise BadRequestError("person_id must be a non-blank person identifier")
    try:
        canonical = normalize_optional_locale(locale)
    except InvalidLocaleError as exc:
        raise BadRequestError(str(exc)) from exc
    _require_backend()
    person = await _pkg._person_store().set_locale(person_id, canonical)
    if person is None:
        raise NotFoundError(f"conversation person not found: {person_id!r}")
    return person.model_dump(mode="json")
