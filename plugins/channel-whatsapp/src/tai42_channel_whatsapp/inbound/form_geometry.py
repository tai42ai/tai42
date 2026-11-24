"""The screen geometry of an endpoint-driven WhatsApp form, as its published Flow was built.

Every ``data_exchange`` step reads the form through the same geometry the published Flow and its
send data were built from: the form's properties, its resolved pages, every screen's field names
and its ``END -> START`` range merge map.
"""

from __future__ import annotations

from typing import Any

from tai42_channel_whatsapp.correlation import EndpointForm
from tai42_channel_whatsapp.flows import page_screen_id
from tai42_channel_whatsapp.flows_resolve import form_range_merges, resolve_pages


def form_geometry(
    form: EndpointForm,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[list[str]], dict[str, str]]:
    """The form's properties, its resolved pages, every screen's field names and its range merge map."""
    properties = form.schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    option_fields = set(form.option_fields)
    resolved, fields_by_screen = resolve_pages(properties, form.pages, option_fields)
    merges = form_range_merges(properties, resolved, fields_by_screen, option_fields)
    return properties, resolved, fields_by_screen, merges


def screen_fields(form: EndpointForm, screen: str) -> tuple[list[list[str]], int | None]:
    """Every screen's field names and the index of ``screen`` among them (``None`` when not a form screen)."""
    _, _, fields_by_screen, _ = form_geometry(form)
    current = next((index for index in range(len(fields_by_screen)) if page_screen_id(index) == screen), None)
    return fields_by_screen, current
