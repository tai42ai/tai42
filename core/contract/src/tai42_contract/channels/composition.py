"""The shared cross-field rule every option-carrying message carrier enforces.

``check_interactive_composition`` is the one place a carrier's message/media/location/
template/options/sections/schema/header/footer combination is judged, so no two carriers
can drift.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels.options import Option, OptionSection
from tai42_contract.channels.templates import ChannelTemplate
from tai42_contract.interactions.models import (
    LocationElement,
    MediaItem,
    Wording,
    check_form_property_names,
    check_wording_for_notification,
)


def _require_prompt_for_surfaces(
    message: str,
    media: list[MediaItem] | None,
    location: LocationElement | None,
    options: list[Option] | None,
    sections: list[OptionSection] | None,
    schema: dict[str, Any] | None,
    noun: str,
) -> None:
    # A blank message is admitted ONLY for a content-only send — non-empty ``media`` OR a
    # ``location`` carrying the content; an interactive surface then needs a prompt.
    if not message.strip():
        if not media and location is None:
            raise ValueError(f"message must be non-blank unless media or location carries the {noun} content")
        if options is not None or sections is not None:
            raise ValueError(f"a content-only (blank-message) {noun} carries no options; a choice needs a prompt")
        if schema is not None:
            raise ValueError(f"a content-only (blank-message) {noun} carries no schema; a form needs a prompt")


def _check_surface_exclusivity(
    options: list[Option] | None,
    sections: list[OptionSection] | None,
    schema: dict[str, Any] | None,
    noun: str,
) -> None:
    # One choice surface: ``options`` XOR ``sections``; ``schema`` excludes both.
    if options is not None and sections is not None:
        raise ValueError(f"options and sections are mutually exclusive on one {noun}")
    if schema is not None and options is not None:
        raise ValueError(f"schema and options are mutually exclusive on one {noun}")
    if schema is not None and sections is not None:
        raise ValueError(f"schema and sections are mutually exclusive on one {noun}")


def _check_header_footer_require_surface(
    header: MediaItem | None,
    footer: str | None,
    options: list[Option] | None,
    sections: list[OptionSection] | None,
    noun: str,
) -> None:
    # A header/footer composes an interactive message, so each requires a choice surface
    # (``options``/``sections``).
    if header is not None and options is None and sections is None:
        raise ValueError(f"a header requires options or sections on the {noun}")
    if footer is not None and options is None and sections is None:
        raise ValueError(f"a footer requires options or sections on the {noun}")


def _check_template_standalone(
    template: ChannelTemplate | None,
    media: list[MediaItem] | None,
    location: LocationElement | None,
    options: list[Option] | None,
    sections: list[OptionSection] | None,
    schema: dict[str, Any] | None,
    header: MediaItem | None,
    footer: str | None,
    noun: str,
) -> None:
    # A template is the standalone out-of-window send: exclusive with every other content
    # and interactive field.
    if template is not None:
        for field, present in (
            ("media", media is not None),
            ("location", location is not None),
            ("options", options is not None),
            ("sections", sections is not None),
            ("schema", schema is not None),
            ("header", header is not None),
            ("footer", footer is not None),
        ):
            if present:
                raise ValueError(f"{field} and template are mutually exclusive on one {noun}")


def check_interactive_composition(
    *,
    message: str,
    media: list[MediaItem] | None,
    location: LocationElement | None,
    template: ChannelTemplate | None,
    options: list[Option] | None,
    sections: list[OptionSection] | None,
    schema: dict[str, Any] | None,
    header: MediaItem | None,
    footer: str | None,
    wording: Wording | None,
    has_pages: bool,
    noun: str,
) -> None:
    """The shared cross-field rules every option-carrying message carrier enforces.

    :class:`ChannelNotification` and :class:`~tai42_contract.conversations.AnswerPart` share these,
    so the two can never drift. ``noun`` names the carrier for the raised messages
    (``"notification"`` / ``"part"``). Rules:

    * message is non-blank BY DEFAULT, EXCEPT blank for a CONTENT-ONLY send — a blank message
      carried by non-empty ``media`` OR a ``location`` (a caption-less image / a bare pin).
    * an interactive surface (``options``, ``sections``, ``schema``) REQUIRES a non-blank message
      — a choice or a form needs a prompt — so a content-only send carries none of them.
    * ``options`` XOR ``sections`` — one choice surface (flat buttons OR a sectioned list).
    * ``schema`` is exclusive with both ``options`` and ``sections`` — one interactive surface
      (a form's fields OR a choice list), never both.
    * ``header`` and ``footer`` compose an interactive message, so each REQUIRES ``options`` or
      ``sections`` present.
    * each per-send ``wording`` member is admitted only on the surface that draws it — a form
      (``schema``), a flat ``options`` list, or a sectioned ``sections`` list (the one admission
      table, :func:`~tai42_contract.interactions.models.wording.check_wording_for_notification`);
      ``wording.form.submit`` is refused when the form carries ``pages`` (``has_pages``).
    * ``template`` is the standalone out-of-window send: exclusive with every other content and
      interactive field (``media``, ``location``, ``options``, ``sections``, ``schema``; and
      transitively ``header``/``footer``, which require ``options``/``sections``).
    * a form ``schema`` declares no top-level property named ``__proto__``
      (:func:`~tai42_contract.interactions.models.forms.check_form_property_names`), the one
      name a form drawn in a browser cannot keep — the same refusal an asked form meets.
    """
    _require_prompt_for_surfaces(message, media, location, options, sections, schema, noun)
    _check_surface_exclusivity(options, sections, schema, noun)
    _check_header_footer_require_surface(header, footer, options, sections, noun)
    _check_template_standalone(template, media, location, options, sections, schema, header, footer, noun)
    if isinstance(schema, dict):
        check_form_property_names(schema)
    if wording is not None:
        check_wording_for_notification(
            wording,
            has_schema=schema is not None,
            has_options=options is not None,
            has_sections=sections is not None,
            has_pages=has_pages,
        )
