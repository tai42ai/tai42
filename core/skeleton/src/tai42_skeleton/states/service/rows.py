"""Paging clamps, store-row → model codecs, and authored-schema-body resolution.

The low helpers the service mixins share: the listing page-size clamp, the store row to
listing/declaration projections, and the resolution of a declaration's authored base
``schema`` (the ``TemplatedText | dict`` union) to the plain schema dict every composition,
validation and narrowing check works on.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter
from tai42_contract.states.errors import ValueValidationError
from tai42_contract.states.models import StateDeclaration
from tai42_contract.template import TemplatedText
from tai42_kit.utils.render import resolve_schema_body

_DEFAULT_PAGE = 200
_MAX_PAGE = 500

_SCHEMA_BODY_ADAPTER = TypeAdapter(TemplatedText | dict[str, Any])


async def _resolve_state_schema(field: str, stored: TemplatedText | dict[str, Any]) -> dict[str, Any]:
    """Resolve a state's authored base ``schema`` — the ``TemplatedText | dict`` union — to the
    plain schema dict every composition, validation and narrowing check works on.

    An in-memory value already typed by the declaration model is used as-is; a value read raw
    from the store (a JSON object) is RE-PARSED into the union first, so a stored
    ``{"id": …}`` / ``{"content": …}`` body is recognized as a stored schema reference rather
    than mistaken for an inline schema. A :class:`~tai42_contract.template.TemplatedText` is then
    rendered and parsed to its schema; an unfetchable id or a body that does not render to a JSON
    object raises loudly (naming ``field``), never a silent empty schema."""
    typed = stored if isinstance(stored, TemplatedText) else _SCHEMA_BODY_ADAPTER.validate_python(stored)
    resolved = await resolve_schema_body(field, typed)
    assert resolved is not None  # a base schema is never None (its unset value is an empty dict)
    return resolved


def _page_limit(limit: Any) -> int:
    """The clamped page size for the listing/search doors — ``None`` takes the default; a
    non-positive or non-integer limit is a loud client error; anything above the hard cap
    is clamped."""
    if limit is None:
        return _DEFAULT_PAGE
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueValidationError(f"limit must be a positive integer, got {limit!r}")
    return min(limit, _MAX_PAGE)


def _subject_from_row(state: str, row: dict[str, Any]) -> dict[str, Any]:
    """A store subject row → the listing dict ``{subject: {…}, updated_at}``."""
    return {
        "subject": {
            "target_kind": row["target_kind"],
            "target_name": row["target_name"],
            "kind": row["subject_kind"],
            "key": row["subject_key"],
        },
        "updated_at": row["updated_at"],
    }


def _row_to_declaration(row: dict[str, Any]) -> StateDeclaration:
    return StateDeclaration(
        name=row["name"],
        description=row.get("description") or "",
        schema=row["schema"],
        subject_kinds=list(row["subject_kinds"]),
        default_subject_kind=row["default_subject_kind"],
        retention_days=row.get("retention_days"),
        effective_schema=row.get("effective_schema"),
        updated_at=row.get("updated_at"),
    )
