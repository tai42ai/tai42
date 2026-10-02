"""This channel's OWN template-parameter schema: the shape of the opaque ``ChannelTemplate.parameters``.

The platform threads ``ChannelTemplate.parameters`` unopened; this channel owns its shape. A
template's runtime arguments are its NAMED components, mapped one-to-one onto Meta's
template-message ``components`` array by :mod:`tai42_channel_whatsapp.client`:

* ``header_media`` — the media argument for a media HEADER component (a display item:
  image/document/video, never a ``link`` or ``audio``); ``None`` when the template has no media
  header (or a static/text header needing no argument).
* ``body_parameters`` — the POSITIONAL body-text values substituted into the body's placeholders in
  order; empty when the body has no placeholders. Typed values (currency, date-time) ride as their
  pre-formatted STRING here.
* ``buttons`` — the POSITIONAL per-button arguments of the buttons component (a quick-reply payload
  or a url suffix); the i-th entry parameterises the i-th button, at most ``TEMPLATE_BUTTONS_MAX``.
  Empty when no button needs a runtime argument.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from tai42_contract.interactions.models import MediaItem, MediaKind

# Caps on a template's component parameters, per Meta's template-message API.
# ``TEMPLATE_BUTTONS_MAX`` bounds the buttons component; ``TEMPLATE_PARAM_MAX_CHARS`` bounds one
# substituted value (a body text param, a button payload, a URL suffix) — a generous single-value
# bound, never a message body.
TEMPLATE_BUTTONS_MAX = 10
TEMPLATE_PARAM_MAX_CHARS = 4096


class QuickReplyButtonParam(BaseModel):
    """The runtime argument for one QUICK-REPLY button of a template's buttons component.

    ``payload`` is the string the medium returns when the human taps the button. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["quick_reply"] = "quick_reply"
    payload: str

    @field_validator("payload")
    @classmethod
    def _payload_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quick-reply button payload must be non-blank")
        if len(value) > TEMPLATE_PARAM_MAX_CHARS:
            raise ValueError(
                f"quick-reply button payload must be at most {TEMPLATE_PARAM_MAX_CHARS} characters, got {len(value)}"
            )
        return value


class UrlButtonParam(BaseModel):
    """The runtime argument for one URL button of a template's buttons component.

    ``url_parameter`` is the dynamic suffix substituted into the button's pre-approved URL. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["url"] = "url"
    url_parameter: str

    @field_validator("url_parameter")
    @classmethod
    def _url_parameter_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url button parameter must be non-blank")
        if len(value) > TEMPLATE_PARAM_MAX_CHARS:
            raise ValueError(
                f"url button parameter must be at most {TEMPLATE_PARAM_MAX_CHARS} characters, got {len(value)}"
            )
        return value


# One button's runtime argument on a template's buttons component: a quick-reply payload or a URL
# suffix, discriminated on ``kind``. Positional — the i-th entry parameterises the i-th button.
TemplateButtonParam = Annotated[QuickReplyButtonParam | UrlButtonParam, Field(discriminator="kind")]


def _no_button_params() -> list[TemplateButtonParam]:
    # A concretely-typed default factory for the buttons component (an empty list default over the
    # discriminated ``TemplateButtonParam`` alias), so the field's element type stays fully known.
    return []


class WhatsAppTemplateParameters(BaseModel):
    """This channel's typed view of a template's opaque runtime arguments.

    Unknown keys are rejected: a parameter shape this channel could not map to Meta's components is a
    caller bug, refused loudly rather than silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    header_media: MediaItem | None = None
    body_parameters: list[str] = Field(default_factory=list)
    buttons: list[TemplateButtonParam] = Field(default_factory=_no_button_params)

    @field_validator("header_media")
    @classmethod
    def _header_media_valid(cls, value: MediaItem | None) -> MediaItem | None:
        if value is not None and value.kind is MediaKind.LINK:
            raise ValueError("template header_media must be a display item (image/document/video/audio), not a link")
        return value

    @field_validator("body_parameters")
    @classmethod
    def _body_parameters_valid(cls, value: list[str]) -> list[str]:
        for param in value:
            if not param.strip():
                raise ValueError("each body parameter must be non-blank")
            if len(param) > TEMPLATE_PARAM_MAX_CHARS:
                raise ValueError(
                    f"each body parameter must be at most {TEMPLATE_PARAM_MAX_CHARS} characters, got {len(param)}"
                )
        return value

    @field_validator("buttons")
    @classmethod
    def _buttons_capped(cls, value: list[TemplateButtonParam]) -> list[TemplateButtonParam]:
        if len(value) > TEMPLATE_BUTTONS_MAX:
            raise ValueError(f"buttons carries at most {TEMPLATE_BUTTONS_MAX} entries, got {len(value)}")
        return value


def parse_template_parameters(parameters: dict[str, Any] | None) -> WhatsAppTemplateParameters:
    """Parse the opaque ``ChannelTemplate.parameters`` into this channel's typed model.

    ``None`` means the template takes no runtime arguments. A parameter shape this channel could not
    map raises ``pydantic.ValidationError`` / ``ValueError``.
    """
    if parameters is None:
        return WhatsAppTemplateParameters()
    return WhatsAppTemplateParameters.model_validate(parameters)
