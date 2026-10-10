"""The reserved settings-profile names: the apply pipeline's own snapshots carry the reserved prefix."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, RootModel

RESERVED_PROFILE_PREFIX: Final = "@"
"""A profile name starting with it is reserved for the apply pipeline; a user may not create one."""

PREVIOUS_PROFILE_NAME: Final = "@previous"
"""The rollback snapshot an apply saves the pre-apply stored env into; applying it rolls the apply back."""


class SettingsProfileName(RootModel[Annotated[str, Field(min_length=1, pattern=f"^[^{RESERVED_PROFILE_PREFIX}]")]]):
    """A user-created profile name: non-empty, not starting with the reserved prefix."""


class PreviousProfileName(RootModel[Literal["@previous"]]):
    """The rollback snapshot's name."""
