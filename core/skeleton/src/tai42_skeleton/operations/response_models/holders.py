"""Response models for the holder-preview doors: every live holder an action would be refused on."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from tai42_contract.tools import ReferenceHolder


class HoldersPreview(BaseModel):
    """Every live holder the named action would be refused on — the same gather its gate reads."""

    action: Literal["rename", "delete", "detach"]
    holders: list[ReferenceHolder]
