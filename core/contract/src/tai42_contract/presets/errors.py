"""Errors the preset typed view raises.

The concrete skeleton view maps the generic
:mod:`tai42_contract.versioning.errors` to these preset-specific types, plus
:class:`PresetNameConflictError` (a preset-only concern — the store never sees a
tool-name collision). Each carries the preset ``name`` it concerns.
"""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class PresetError(Exception):
    """Base for preset view failures, carrying the preset ``name``."""

    # A bare view failure is an unclassified store fault (subclasses stamp their own).
    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name


class PresetNotFoundError(PresetError):
    """No preset (or no active preset) named ``name``."""

    # The addressed preset does not exist.
    __tai_error_kind__ = ErrorKind.NOT_FOUND

    def __init__(self, name: str):
        super().__init__(name, f"preset {name!r} not found")


class PresetExistsError(PresetError):
    """A preset named ``name`` already exists."""

    # A create colliding with an existing preset.
    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, name: str):
        super().__init__(name, f"preset {name!r} already exists")


class PresetVersionNotFoundError(PresetError):
    """No version ``version`` exists for preset ``name``."""

    # A requested preset version does not exist.
    __tai_error_kind__ = ErrorKind.NOT_FOUND

    def __init__(self, name: str, version: int | None = None):
        self.version = version
        detail = "" if version is None else f" version {version}"
        super().__init__(name, f"preset {name!r} has no{detail} version")


class PresetNameConflictError(PresetError):
    """The preset ``name`` collides with an existing base tool.

    A preset must never silently shadow a real tool, so the register/create path
    raises this loudly rather than overwriting the tool.
    """

    # The requested name collides with an existing tool — a naming conflict.
    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, name: str):
        super().__init__(name, f"preset name {name!r} collides with an existing tool")
