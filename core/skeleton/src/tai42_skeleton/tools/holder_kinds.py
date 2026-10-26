"""The ``ReferenceHolder`` kinds the platform's own referees and gates answer, each with its feature tag.

A plugin declares its own kinds; these are the platform's. A kind's tag is the feature tag of
the operations that list holders of that kind; a caller whose role may not read a kind's tag
reads its holders as a count. Where a kind's ``name`` or ``locator`` carries more than the
holder's own name, the constant says so.
"""

from __future__ import annotations

from typing import Final, NamedTuple

from tai42_contract.tools import ReferenceHolder


class HolderKind(NamedTuple):
    """A holder kind and the feature tag its holders live under."""

    kind: str
    tag: str

    def holder(self, *, name: str, summary: str, locator: dict[str, str | int] | None = None) -> ReferenceHolder:
        """One holder of this kind under this kind's tag."""
        return ReferenceHolder(kind=self.kind, tag=self.tag, name=name, locator=locator or {}, summary=summary)


#: A stored preset that holds the name (composes it). ``name`` is the preset.
PRESET: Final = HolderKind("preset", "presets")
#: One version of a stored preset. ``name`` is the preset; locator ``version``.
PRESET_VERSION: Final = HolderKind("preset version", "presets")
#: A hook. ``name`` is the hook.
HOOK: Final = HolderKind("hook", "hooks")
#: A schedule. ``name`` is the schedule.
SCHEDULE: Final = HolderKind("schedule", "schedules")
#: A conversation route. ``name`` is the route.
CONVERSATION_ROUTE: Final = HolderKind("conversation route", "conversations")
#: A conversation target config. ``name`` is the target name; locator ``target_kind``.
CONVERSATION_CONFIG: Final = HolderKind("conversation config", "conversations")
#: The manifest's tool-extensions map entry. ``name`` is the tool the entry is keyed on.
TOOL_EXTENSIONS: Final = HolderKind("tool extensions", "extensions")
#: Open parks resuming into a tool. ``name`` is the tool; locator ``count``.
PARKED_INTERACTIONS: Final = HolderKind("parked interactions", "interactions")
#: Answered continuations awaiting redelivery into a tool. ``name`` is the tool; locator ``count``.
ANSWERED_CONTINUATIONS: Final = HolderKind("answered continuations", "interactions")
#: State records under a ``tool`` target. ``name`` is the tool; locator ``count``.
STATE_RECORDS: Final = HolderKind("state records", "states")
#: One outstanding pending state save. ``name`` is the save's id; locator ``status``.
PENDING_SAVE: Final = HolderKind("pending save", "states")
#: A setting that names the tool or the stored template. ``name`` is the setting's environment variable.
SETTING: Final = HolderKind("setting", "config")
#: A state declaration. ``name`` is the state.
STATE_DECLARATION: Final = HolderKind("state declaration", "states")
#: A state template document. ``name`` is the template.
STATE_TEMPLATE: Final = HolderKind("state template", "states")
#: An access-control policy row minted as an api key. ``name`` is the key's user id.
API_KEY: Final = HolderKind("api key", "access-control")
#: An access-control policy row of a principal. ``name`` is the principal's user id.
PRINCIPAL: Final = HolderKind("principal", "access-control")
#: A role. ``name`` is the role.
ROLE: Final = HolderKind("role", "access-control")

__all__ = [
    "ANSWERED_CONTINUATIONS",
    "API_KEY",
    "CONVERSATION_CONFIG",
    "CONVERSATION_ROUTE",
    "HOOK",
    "PARKED_INTERACTIONS",
    "PENDING_SAVE",
    "PRESET",
    "PRESET_VERSION",
    "PRINCIPAL",
    "ROLE",
    "SCHEDULE",
    "SETTING",
    "STATE_DECLARATION",
    "STATE_RECORDS",
    "STATE_TEMPLATE",
    "TOOL_EXTENSIONS",
    "HolderKind",
]
