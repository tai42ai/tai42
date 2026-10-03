"""Pre-approved out-of-window template send: a named template reference + opaque channel-validated parameters."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class ChannelTemplate(BaseModel):
    """A pre-approved, named template a channel sends outside its freeform window.

    Some media only accept arbitrary text inside a bounded conversation window (a provider's
    customer-service window); outside it, the sole accepted send is an operator-authored,
    pre-approved template referenced by ``name`` in an approved ``language`` locale — both
    required and non-blank.

    ``parameters`` is the template's runtime arguments as an OPAQUE object: the platform
    threads it UNOPENED (never looking inside it), and the channel that declares the
    ``supports_template_notifications`` capability owns its shape — it validates ``parameters``
    against its OWN template-parameter schema through its OPTIONAL ``validate_template`` hook
    (see :class:`~tai42_contract.channels.Channel`) and maps it to its own send API in its own
    code. A template with no runtime arguments carries no ``parameters``; a present
    ``parameters`` is a non-empty dict.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    language: str
    parameters: dict[str, Any] | None = None

    @field_validator("name", "language")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-blank")
        return value

    @field_validator("parameters")
    @classmethod
    def _parameters_non_empty(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        # None means the template takes no runtime arguments; an empty dict is a caller bug (a
        # present ``parameters`` must carry something). The channel's ``validate_template`` hook
        # owns the deep shape — this is only the non-empty bound, the same the form ``schema``
        # keeps.
        if value is not None and not value:
            raise ValueError("template parameters must be a non-empty dict when present")
        return value
