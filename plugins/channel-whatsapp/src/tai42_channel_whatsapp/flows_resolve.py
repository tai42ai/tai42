"""Resolving a form's page layout and its range-pair merges.

A form's screens come from its ``pages`` (absent → one screen carrying every
property in schema order); the range-pair merge map says which ``rangeStart`` date
pairs draw as a single range control. Both the Flow-JSON builder and the per-send
data builder read this shared geometry, so a pair's merge decision and a screen's
field list are identical on every surface.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows_components import range_pairs

# The default top-bar title of the page-less form's single screen, shown when the
# caller passes no ``form_title``. A form ask carries no domain-specific chrome.
_SCREEN_TITLE = "Form"


def _resolve_pages(
    properties: dict[str, Any],
    pages: list[dict[str, Any]] | None,
    form_title: str | None = None,
    form_submit: str | None = None,
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """The resolved page list and each page's field names.

    Defaults to one screen carrying every property in schema order, its top-bar ``title`` the
    caller's ``form_title`` when set (else the generic :data:`_SCREEN_TITLE`) and its terminal
    ``button_label`` the caller's ``form_submit`` when set (else the footer's generic default). A
    form WITH ``pages`` shows each step's own page ``title`` and ``button_label``, so ``form_title``
    and ``form_submit`` apply only to the page-less default. Raises ``ChannelInputError`` for a page
    that names a property the schema does not declare.
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
        fields_by_screen.append(page_fields)
    return resolved_pages, fields_by_screen


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
