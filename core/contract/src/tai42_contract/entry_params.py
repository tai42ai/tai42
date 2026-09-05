"""Opaque enrichment-params transport — the one shared vocabulary for the string-keyed,
string-valued enrichment a channel adapter carries alongside a turn or an answer.

The platform attaches NO meaning and NO TRUST to these params; it only bounds the transport
(count, key shape, value length, total serialized size). The SAME bounds guard every seam that
carries channel enrichment: a conversation entry (:class:`~tai42_contract.conversations.ConversationMessage`
and ``AppConversations.accept``), the bridged/answered inbound seam
(:class:`~tai42_contract.channels.InboundBridge`), and the answer envelope
(:class:`~tai42_contract.interactions.models.InteractionResponse`) — so the bridge path and the
answer path admit exactly the same enrichment. This module sits below channels/conversations/
interactions so all three reuse one validator with no import cycle; ``conversations`` re-exports
the names for the public ``tai42_contract.conversations`` surface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import cast

# Entry-param transport bounds. The platform carries opaque caller-supplied params and attaches
# NO meaning and NO trust; these bounds cap the transport alone (count, key shape, value length,
# total serialized size).
ENTRY_PARAMS_MAX_COUNT = 16
ENTRY_PARAM_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENTRY_PARAM_VALUE_MAX_CHARS = 512
ENTRY_PARAMS_MAX_TOTAL_BYTES = 2048  # len(json.dumps(params, separators=(",", ":"), sort_keys=True).encode())


def validate_entry_params(params: dict[str, str]) -> dict[str, str]:
    """Refuse (``ValueError`` naming the first violated bound) or return the dict unchanged.

    Checks, in order: count, key regex, value type is ``str``, value length, total
    serialized bytes. The message names the violated bound and MAY name the offending KEY,
    but NEVER a value — an entry-param value is opaque and must never surface in a log or an
    error. Duplicate keys cannot reach a dict; the web door refuses duplicates at parse time
    before building the dict.
    """
    if len(params) > ENTRY_PARAMS_MAX_COUNT:
        raise ValueError(f"params carries {len(params)} keys, over the {ENTRY_PARAMS_MAX_COUNT} allowed")
    # Values are validated as untrusted objects: the annotation promises ``str``, but a
    # non-str value would serialize silently through ``json.dumps`` below, so the type is
    # enforced loudly here (the object view keeps the check genuine, not redundant).
    untrusted = cast("Mapping[str, object]", params)
    for key, value in untrusted.items():
        if not ENTRY_PARAM_KEY_RE.fullmatch(key):
            raise ValueError(f"params key {key!r} must match {ENTRY_PARAM_KEY_RE.pattern!r}")
        if not isinstance(value, str):
            raise ValueError(f"params value for key {key!r} must be a string")
        if len(value) > ENTRY_PARAM_VALUE_MAX_CHARS:
            raise ValueError(f"params value for key {key!r} is over the {ENTRY_PARAM_VALUE_MAX_CHARS}-character limit")
    total_bytes = len(json.dumps(params, separators=(",", ":"), sort_keys=True).encode())
    if total_bytes > ENTRY_PARAMS_MAX_TOTAL_BYTES:
        raise ValueError(f"params serialize to {total_bytes} bytes, over the {ENTRY_PARAMS_MAX_TOTAL_BYTES} allowed")
    return params


__all__ = [
    "ENTRY_PARAMS_MAX_COUNT",
    "ENTRY_PARAMS_MAX_TOTAL_BYTES",
    "ENTRY_PARAM_KEY_RE",
    "ENTRY_PARAM_VALUE_MAX_CHARS",
    "validate_entry_params",
]
