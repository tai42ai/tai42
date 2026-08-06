"""Remediation wording for a component's unconfigured store.

A store-owning door refuses with a 501 whose message names the exact env var that
turns its store on — derived from the LIVE binding of the skeleton component
(``TAI_DB_BINDING_SKELETON``, ``default`` when unset) at call time, so a rebound
component names its real database rather than a frozen default. Reuses kit's
env-name construction so the var name has one source of truth.
"""

from __future__ import annotations

from tai42_kit.db import component_binding, database_password_env

from tai42_skeleton.db.discovery import SKELETON_COMPONENT


def not_configured_message(noun: str) -> str:
    """The 501 remediation for an unconfigured ``noun``: name the ``PG_PASSWORD`` env
    var of the database the skeleton component is bound to, read live per call."""
    env_var = database_password_env(component_binding(SKELETON_COMPONENT))
    return f"the {noun} is not configured: set {env_var}"
