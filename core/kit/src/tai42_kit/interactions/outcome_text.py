"""The text rendering of a failed-terminal outcome, for a door that delivers it as text."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = ["failed_outcome_text"]


def failed_outcome_text(outcome: Mapping[str, Any]) -> str:
    r"""The whole failed-terminal ``outcome`` rendered as JSON text, for a TEXT delivery door.

    A door that delivers a failed terminal as human/model-readable TEXT — an agent's own tool call,
    whose failure the loop turns into an error tool message — renders the WHOLE outcome here: the
    opaque payload reproduced as JSON, never reading a key inside it and naming no driver or engine.
    A door that delivers structured data carries the mapping itself instead and does not use this.

    ``ensure_ascii=False`` so non-ASCII text survives as itself rather than as ``\uXXXX`` escapes.
    An outcome no JSON encoder can render raises loudly (``json.dumps`` raises :class:`TypeError` for
    a non-serializable member): the failure is surfaced, never dropped nor a character rewritten to
    get past it.
    """
    return json.dumps(outcome, ensure_ascii=False)
