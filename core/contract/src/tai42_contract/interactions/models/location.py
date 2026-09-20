"""The shared geographic-point model a message may carry.

``LocationElement`` is one point used both ways — an outbound place the sender shares and
the inbound location a participant sent — with its optional-label caps.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

# Caps on a shared location element's optional labels — a place name and a street address, each
# a short single-line human label the medium renders beside the pin, not a message body.
LOCATION_NAME_MAX_CHARS = 1000
LOCATION_ADDRESS_MAX_CHARS = 1000


class LocationElement(BaseModel):
    """A geographic point shared on a message — the one shape used BOTH ways.

    It is an outbound place the sender shares and the inbound location a participant sent.
    ``latitude``/``longitude`` are WGS84 decimal degrees, bounded to their valid ranges
    (latitude -90..90, longitude -180..180). ``name`` is an optional place label and ``address``
    an optional street address, each a single-line non-blank string within its cap when present
    (raw whitespace and control/format characters are rejected, as on a media url — an embedded
    newline or bidi spoof would corrupt the rendered pin label). A channel that cannot render a
    map renders the coordinates (and any name/address) as text. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None

    @field_validator("latitude")
    @classmethod
    def _check_latitude(cls, value: float) -> float:
        if not -90.0 <= value <= 90.0:
            raise ValueError(f"latitude must be within -90..90 degrees, got {value}")
        return value

    @field_validator("longitude")
    @classmethod
    def _check_longitude(cls, value: float) -> float:
        if not -180.0 <= value <= 180.0:
            raise ValueError(f"longitude must be within -180..180 degrees, got {value}")
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("location name must be non-blank when present")
            if len(value) > LOCATION_NAME_MAX_CHARS:
                raise ValueError(
                    f"location name must be at most {LOCATION_NAME_MAX_CHARS} characters, got {len(value)}"
                )
            if any((ch.isspace() and ch != " ") or not ch.isprintable() for ch in value):
                raise ValueError("location name must be a single-line label with no control characters")
        return value

    @field_validator("address")
    @classmethod
    def _check_address(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("location address must be non-blank when present")
            if len(value) > LOCATION_ADDRESS_MAX_CHARS:
                raise ValueError(
                    f"location address must be at most {LOCATION_ADDRESS_MAX_CHARS} characters, got {len(value)}"
                )
            if any((ch.isspace() and ch != " ") or not ch.isprintable() for ch in value):
                raise ValueError("location address must be a single-line label with no control characters")
        return value
