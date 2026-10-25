"""The ``ReferenceHolder.kind`` values the platform's own referees and gates answer.

A plugin declares its own kinds; these are the platform's. Where a kind's ``name`` or
``locator`` carries more than the holder's own name, the constant says so.
"""

from __future__ import annotations

from typing import Final

#: A stored preset that holds the name (composes it). ``name`` is the preset.
PRESET: Final = "preset"
#: One version of a stored preset. ``name`` is the preset; locator ``version``.
PRESET_VERSION: Final = "preset version"
#: A hook. ``name`` is the hook.
HOOK: Final = "hook"
#: A schedule. ``name`` is the schedule.
SCHEDULE: Final = "schedule"
#: A conversation route. ``name`` is the route.
CONVERSATION_ROUTE: Final = "conversation route"
#: A conversation target config. ``name`` is the target name; locator ``target_kind``.
CONVERSATION_CONFIG: Final = "conversation config"
#: The manifest's tool-extensions map entry. ``name`` is the tool the entry is keyed on.
TOOL_EXTENSIONS: Final = "tool extensions"
#: Open parks resuming into a tool. ``name`` is the tool; locator ``count``.
PARKED_INTERACTIONS: Final = "parked interactions"
#: Answered continuations awaiting redelivery into a tool. ``name`` is the tool; locator ``count``.
ANSWERED_CONTINUATIONS: Final = "answered continuations"
#: State records under a ``tool`` target. ``name`` is the tool; locator ``count``.
STATE_RECORDS: Final = "state records"
#: One outstanding pending state save. ``name`` is the save's id; locator ``status``.
PENDING_SAVE: Final = "pending save"
#: A setting that names the tool or the stored template. ``name`` is the setting's environment variable.
SETTING: Final = "setting"
#: A state declaration. ``name`` is the state.
STATE_DECLARATION: Final = "state declaration"
#: A state template document. ``name`` is the template.
STATE_TEMPLATE: Final = "state template"
#: An access-control policy row minted as an api key. ``name`` is the key's user id.
#: A caller who may not read the identity lists reads one holder per (stored id, site)
#: instead: ``name`` is the id and locator ``count`` the number of keys.
API_KEY: Final = "api key"
#: An access-control policy row of a principal. ``name`` is the principal's user id, or
#: the stored id with locator ``count`` for a caller who may not read the identity lists.
PRINCIPAL: Final = "principal"
#: A role. ``name`` is the role, or the stored id with locator ``count`` for a caller who
#: may not read the identity lists.
ROLE: Final = "role"

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
]
