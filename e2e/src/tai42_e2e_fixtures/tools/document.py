"""The document probe: a large structured document built inside the serving process.

A chain branch over ``e2e_document`` hands the document to its jq program as parsed
structure, so a request carries a large jq input end to end without sending it over the
wire. The content is deterministic: the same ``items`` always renders the same text."""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

_TAGS = ("alpha", "beta", "gamma", "delta")
_TEXT = "the quick brown fox jumps over the lazy dog " * 3


def _item(index: int) -> dict[str, Any]:
    return {
        "id": index,
        "name": f"item-{index}",
        "tags": list(_TAGS),
        "attributes": {
            "label": f"label-{index:06d}",
            "rank": index % 97,
            "group": f"group-{index % 16}",
            "enabled": index % 2 == 0,
            "text": _TEXT,
            "nested": {"x": index % 7, "y": index % 11, "z": f"z-{index % 5}"},
        },
    }


@tai42_app.tools.tool(tags={"e2e"})
def e2e_document(items: int) -> dict[str, Any]:
    """A structured document of ``items`` entries — each an ``id``, a ``name``, a list of
    ``tags`` and a nested ``attributes`` object, about 350 bytes of JSON per entry — built in
    the serving process, so a chained jq program runs over a large parsed document that never
    crossed the wire."""
    return {"items": [_item(index) for index in range(items)]}
