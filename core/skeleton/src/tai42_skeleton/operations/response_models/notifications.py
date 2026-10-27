"""Response models for the notifications operations: the internal notifications feed and the notify result."""

from __future__ import annotations

import warnings

from pydantic import BaseModel, JsonValue, RootModel
from tai42_contract.channels import ChannelTemplate, Option, OptionSection
from tai42_contract.interactions.models import LocationElement, MediaItem

with warnings.catch_warnings():
    # ``schema`` intentionally shadows pydantic's deprecated ``BaseModel.schema``
    # alias: it is the stored record's wire key (an ask-less form's answer schema),
    # so the field name must match. Suppressed narrowly at the definition site, the
    # same pattern the ``NotifyUser`` request model uses for its ``schema`` field.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class NotificationRecord(BaseModel):
        """One stored internal-sink notification.

        ``recipient``/``audience`` and the richer-send forms
        (``media``/``template``/``options``/``location``/``sections``/``header``/
        ``footer``/``schema``) are ``null`` on a plain record; ``schema`` (an open
        answer-schema object) rides only from the channel path's feed write.
        ``id``/``created_at`` are server-minted.
        """

        id: str
        message: str
        recipient: str | None = None
        audience: str | None = None
        media: list[MediaItem] | None = None
        template: ChannelTemplate | None = None
        options: list[Option] | None = None
        location: LocationElement | None = None
        sections: list[OptionSection] | None = None
        header: MediaItem | None = None
        footer: str | None = None
        schema: JsonValue = None  # pyright: ignore[reportIncompatibleMethodOverride]
        created_at: str


class NotificationListing(BaseModel):
    """The internal notifications feed, newest-first (empty on an unconfigured store)."""

    notifications: list[NotificationRecord]


class NotifyResult(RootModel[str]):
    """The notify-user body: a bare confirmation STRING.

    The adapter envelopes it as ``{"data": <str>}``. Typed as a string rather
    than wrapped, so the wire body is unchanged.
    """
