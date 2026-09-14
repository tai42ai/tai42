"""Pre-approved out-of-window template send: the button params + ``ChannelTemplate`` + caps."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tai42_contract.interactions.models import MediaItem, MediaKind

# Caps on a template's component parameters. ``TEMPLATE_BUTTONS_MAX`` bounds the buttons
# component; ``TEMPLATE_PARAM_MAX_CHARS`` bounds one substituted value (a body text param, a
# button payload, a URL suffix) — a generous single-value bound, never a message body.
TEMPLATE_BUTTONS_MAX = 10
TEMPLATE_PARAM_MAX_CHARS = 4096


class QuickReplyButtonParam(BaseModel):
    """The runtime argument for one QUICK-REPLY button of a template's buttons component:
    ``payload`` is the string the medium returns when the human taps the button. Frozen."""

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
    """The runtime argument for one URL button of a template's buttons component:
    ``url_parameter`` is the dynamic suffix substituted into the button's pre-approved URL.
    Frozen."""

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


class ChannelTemplate(BaseModel):
    """A pre-approved, named template a channel sends outside its freeform window.

    Some media only accept arbitrary text inside a bounded conversation window (e.g. WhatsApp's
    24-hour customer-service window); outside it, the sole accepted send is an operator-authored
    template referenced by ``name`` in an approved ``language`` — both required and non-blank.

    The template's runtime arguments are its NAMED components (never one flat positional list):

    * ``header_media`` — the media argument for a media HEADER component (a display item:
      image/document/video/audio, never a ``link``); ``None`` when the template has no media
      header (or a static/text header needing no argument).
    * ``body_parameters`` — the POSITIONAL body-text values substituted into the body's
      placeholders in order; empty when the body has no placeholders. Typed values (currency,
      date-time) ride as their pre-formatted STRING here — the contract does not model the type.
    * ``buttons`` — the POSITIONAL per-button arguments of the buttons component
      (:data:`TemplateButtonParam`: a :class:`QuickReplyButtonParam` payload or a
      :class:`UrlButtonParam` url suffix); the i-th entry parameterises the i-th button, at most
      ``TEMPLATE_BUTTONS_MAX``. Empty when no button needs a runtime argument.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    language: str
    header_media: MediaItem | None = None
    body_parameters: list[str] = Field(default_factory=list)
    buttons: list[TemplateButtonParam] = Field(default_factory=_no_button_params)

    @field_validator("name", "language")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-blank")
        return value

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
