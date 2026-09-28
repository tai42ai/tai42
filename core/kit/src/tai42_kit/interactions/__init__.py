"""Kit-level interaction helpers a plugin, a backend, or the skeleton share.

The door-contract evaluator lives here (not in the skeleton) so a backend plugin and the
skeleton's own doors reach it identically — kit is the only package both may import.
"""

from tai42_kit.interactions.door_contract import (
    DOOR_START_DEFAULT,
    DoorContractOutcome,
    evaluate_door_contract,
    parked_entries_for_jq,
)
from tai42_kit.interactions.media_caps import (
    MediaIngestCapSettings,
    media_ingest_cap_settings,
)

__all__ = [
    "DOOR_START_DEFAULT",
    "DoorContractOutcome",
    "MediaIngestCapSettings",
    "evaluate_door_contract",
    "media_ingest_cap_settings",
    "parked_entries_for_jq",
]
