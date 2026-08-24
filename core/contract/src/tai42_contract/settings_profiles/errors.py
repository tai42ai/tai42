"""Errors the settings-profile typed view raises.

The concrete skeleton view maps the generic
:mod:`tai42_contract.versioning.errors` to these settings-profile-specific types.
Each carries the profile ``name`` it concerns.
"""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class SettingsProfileError(Exception):
    """Base for settings-profile view failures, carrying the profile ``name``."""

    # A bare view failure is an unclassified store fault (subclasses stamp their own).
    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name


class SettingsProfileNotFoundError(SettingsProfileError):
    """No settings profile (or no active profile) named ``name``."""

    # The addressed profile does not exist.
    __tai_error_kind__ = ErrorKind.NOT_FOUND

    def __init__(self, name: str):
        super().__init__(name, f"settings profile {name!r} not found")


class SettingsProfileExistsError(SettingsProfileError):
    """A settings profile named ``name`` already exists."""

    # A create colliding with an existing profile.
    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, name: str):
        super().__init__(name, f"settings profile {name!r} already exists")


class SettingsProfileVersionNotFoundError(SettingsProfileError):
    """No version ``version`` exists for settings profile ``name``."""

    # A requested profile version does not exist.
    __tai_error_kind__ = ErrorKind.NOT_FOUND

    def __init__(self, name: str, version: int | None = None):
        self.version = version
        detail = "" if version is None else f" version {version}"
        super().__init__(name, f"settings profile {name!r} has no{detail} version")
