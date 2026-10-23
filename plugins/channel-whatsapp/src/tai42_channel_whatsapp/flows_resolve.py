"""Resolving a form's page layout, its range-pair merges and its display slots' data keys.

A form's screens come from its ``pages`` (absent → one screen carrying every
property in schema order); the range-pair merge map says which ``rangeStart`` date
pairs draw as a single range control; the slot map names each display slot's
screen-``data`` key. The Flow-JSON builder, the per-send data builder and the data
endpoint read this shared geometry, so a pair's merge decision, a screen's field
list and a slot's data key are identical on every surface.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels import ChannelInputError
from tai42_kit.interactions import TIME_OF_KEY, drawing_order

from tai42_channel_whatsapp.flows_components import _sanitize_name, range_pairs

# The default top-bar title of the page-less form's single screen, shown when the
# caller passes no ``form_title``. A form ask carries no domain-specific chrome.
_SCREEN_TITLE = "Form"


def _resolve_pages(
    properties: dict[str, Any],
    pages: list[dict[str, Any]] | None,
    form_title: str | None = None,
    form_submit: str | None = None,
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """The resolved page list and each page's field names, in drawing order.

    Defaults to one screen carrying every property in schema order, its top-bar ``title`` the
    caller's ``form_title`` when set (else the generic :data:`_SCREEN_TITLE`) and its terminal
    ``button_label`` the caller's ``form_submit`` when set (else the footer's generic default). A
    form WITH ``pages`` shows each step's own page ``title`` and ``button_label``, so ``form_title``
    and ``form_submit`` apply only to the page-less default. Each page's names are in
    :func:`form_drawing_order`, the one order the screens, the per-send data, the footers and the
    data endpoint read. Raises ``ChannelInputError`` for a page that names a property the schema
    does not declare.
    """
    resolved_pages = pages or [
        {
            "title": form_title or _SCREEN_TITLE,
            "fields": list(properties),
            **({"button_label": form_submit} if form_submit is not None else {}),
        }
    ]
    fields_by_screen: list[list[str]] = []
    for page in resolved_pages:
        page_fields = [str(field) for field in page["fields"]]
        for field in page_fields:
            if field not in properties:
                raise ChannelInputError(f"form page names unknown property {field!r}")
        fields_by_screen.append(drawing_order(page_fields, properties))
    merges = _form_range_merges(properties, resolved_pages, fields_by_screen)
    return resolved_pages, [_place_merged_ends(fields, properties, merges) for fields in fields_by_screen]


def form_drawing_order(names: list[str], properties: dict[str, Any], merges: dict[str, str]) -> list[str]:
    """``names`` in the order a Flow draws them, given the form's ``END -> START`` merge map.

    :func:`~tai42_kit.interactions.drawing_order` (each ``timeOf`` time right after its date), then
    each merged range END, with the times right after it, moved to right after its START and the
    START's own times: the pair draws as one picker at the START, so the END's time follows the
    START's time instead of sitting where the END was declared.
    """
    return _place_merged_ends(drawing_order(names, properties), properties, merges)


def _place_merged_ends(ordered: list[str], properties: dict[str, Any], merges: dict[str, str]) -> list[str]:
    # Move each merged END listed in ``ordered`` (with its ``timeOf`` times right after it) to
    # right after its START's block (the START and its ``timeOf`` times right after it).
    def is_time_of(name: str, date_name: str) -> bool:
        prop = properties.get(name)
        return isinstance(prop, dict) and prop.get(TIME_OF_KEY) == date_name

    def block(names: list[str], head: str) -> list[str]:
        index = names.index(head)
        end = index + 1
        while end < len(names) and is_time_of(names[end], head):
            end += 1
        return names[index:end]

    result = list(ordered)
    for end_name, start_name in merges.items():
        if end_name not in result or start_name not in result:
            continue
        moving = block(result, end_name)
        result = [name for name in result if name not in moving]
        start_block = block(result, start_name)
        at = result.index(start_name) + len(start_block)
        result[at:at] = moving
    return result


def _form_range_merges(
    properties: dict[str, Any], resolved_pages: list[dict[str, Any]], fields_by_screen: list[list[str]]
) -> dict[str, str]:
    """The ``END name -> START name`` merge map for every ``rangeStart`` pair drawn as one control.

    The union of :func:`range_pairs` over every screen's fields — the ONE authority the screen
    builder, the per-send data, the footers and the inbound decode all read, so a pair's merge
    decision is identical everywhere. A form carrying a ``review`` page (whose generic readback reads
    each field's scalar ``__val``) merges no pair, so a merged range object never reaches a text line.
    """
    has_review_page = any(isinstance(page, dict) and page.get("kind") == "review" for page in resolved_pages)
    merges: dict[str, str] = {}
    for this_fields in fields_by_screen:
        merges.update(range_pairs(this_fields, properties, has_review_page))
    return merges


def slot_datanames(pages: list[dict[str, Any]] | None) -> dict[str, str]:
    """Map each declared display ``slot`` to a unique identifier-safe screen-``data`` key.

    A slotted display block reads ``${data.<key>}``; a reaction's ``display`` update fills it
    through the data endpoint. Slot names are unique across a form (the contract enforces it);
    each is sanitised to Meta's identifier grammar and disambiguated deterministically on a
    sanitisation collision, so the renderer and the data endpoint recompute an identical map.
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for page in pages or []:
        for block in page.get("display") or []:
            slot = block.get("slot") if isinstance(block, dict) else None
            if not isinstance(slot, str) or slot in mapping:
                continue
            base = f"slot_{_sanitize_name(slot)}"
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            mapping[slot] = candidate
    return mapping
