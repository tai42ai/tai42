"""Kit-level interaction helpers a plugin, a backend, or the skeleton share.

The door-contract evaluator lives here (not in the skeleton) so a backend plugin and the
skeleton's own doors reach it identically — kit is the only package both may import.
"""

from tai42_kit.interactions.callback_pin import CallbackPinReason, CallbackUrlRefusedError, pin_callback_url
from tai42_kit.interactions.door_contract import (
    DOOR_START_DEFAULT,
    DoorContractOutcome,
    evaluate_door_contract,
    parked_entries_for_jq,
)
from tai42_kit.interactions.form_vocabulary import (
    DATE_CONSTRAINT_KEYS,
    FORM_WEEKDAYS,
    RADIO_OPTION_THRESHOLD,
    VISIBLE_WHEN_OPS,
    evaluate_visible_when,
    hidden_fields,
)
from tai42_kit.interactions.media_caps import (
    MediaIngestCapSettings,
    media_ingest_cap_settings,
)

__all__ = [
    "DATE_CONSTRAINT_KEYS",
    "DOOR_START_DEFAULT",
    "FORM_WEEKDAYS",
    "RADIO_OPTION_THRESHOLD",
    "VISIBLE_WHEN_OPS",
    "CallbackPinReason",
    "CallbackUrlRefusedError",
    "DoorContractOutcome",
    "MediaIngestCapSettings",
    "evaluate_door_contract",
    "evaluate_visible_when",
    "hidden_fields",
    "media_ingest_cap_settings",
    "parked_entries_for_jq",
    "pin_callback_url",
]
