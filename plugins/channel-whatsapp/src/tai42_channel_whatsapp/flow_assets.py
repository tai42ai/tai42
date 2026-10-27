"""The Graph wrappers for a WABA's Flow assets.

:func:`list_flows`, :func:`create_flow`, :func:`publish_flow` and :func:`delete_flow` manage the
Flows under a WhatsApp Business Account; :func:`set_business_public_key` registers the key Meta
encrypts data-endpoint payloads with. Each is a pure Graph wrapper over
:func:`~tai42_channel_whatsapp.graph.send_request` (auth, transport, error policy) on the graph
object endpoints: it returns what Meta answered and decides nothing.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_whatsapp.graph import parse_json, send_request
from tai42_channel_whatsapp.settings import whatsapp_settings

# A loud bound on the flow-list page walk, whose end is the vendor's ``paging.next``.
_MAX_FLOW_LIST_PAGES = 1000


class FlowSummary(NamedTuple):
    """One Flow as Meta's flow list answers it; ``status`` is Meta's spelling (``PUBLISHED`` …)."""

    flow_id: str
    name: str
    status: str


def _flow_list_page(body: Any) -> tuple[list[FlowSummary], str | None]:
    """The Flows on one list page and the ``after`` cursor of the page that follows, if any.

    Meta marks a following page with ``paging.next``; its absence marks the last page.
    Raises ``ChannelDeliveryError`` on a body that is not a JSON object, a ``data`` that
    is not a list, an item without a string ``id``/``name``/``status``, or a ``next``
    without a string ``paging.cursors.after``.
    """
    if not isinstance(body, dict):
        raise ChannelDeliveryError("WhatsApp answered the flow list with a body that is not a JSON object")
    data = body.get("data")
    if not isinstance(data, list):
        raise ChannelDeliveryError("WhatsApp answered the flow list without a data array")
    flows: list[FlowSummary] = []
    for item in data:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("id", "name", "status")):
            raise ChannelDeliveryError(
                "WhatsApp's flow list carries an item without a string id, name and status: "
                + json.dumps(item, sort_keys=True, separators=(",", ":"))[:300]
            )
        flows.append(FlowSummary(flow_id=item["id"], name=item["name"], status=item["status"]))
    paging = body.get("paging", {})
    if not isinstance(paging, dict):
        raise ChannelDeliveryError("WhatsApp answered the flow list with a paging value that is not a JSON object")
    if "next" not in paging:
        return flows, None
    cursors = paging.get("cursors")
    after = cursors.get("after") if isinstance(cursors, dict) else None
    if not isinstance(after, str) or not after:
        raise ChannelDeliveryError("WhatsApp's flow list announces a next page without an after cursor")
    return flows, after


async def list_flows(waba_id: str) -> list[FlowSummary]:
    """Every Flow under ``waba_id``: GET ``{api}/{waba_id}/flows``, walked page by page.

    Each following page is requested on this same URL with ``after=<paging.cursors.after>``,
    never by following the vendor-supplied ``paging.next`` URL, so the Bearer token only
    ever goes to the configured API base. The walk stops on the page without ``next``. A
    pure Graph wrapper: it returns what Meta listed and decides nothing. Raises
    ``ChannelDeliveryError`` on the ``send_request`` failure modes, an unreadable page, or a walk
    past ``_MAX_FLOW_LIST_PAGES`` pages.
    """
    url = f"{whatsapp_settings().api_base_url}/{waba_id}/flows"
    flows: list[FlowSummary] = []
    params: dict[str, str] | None = None
    for _ in range(_MAX_FLOW_LIST_PAGES):
        response = await send_request(url, method="get", params=params)
        page, after = _flow_list_page(parse_json(response))
        flows.extend(page)
        if after is None:
            return flows
        params = {"after": after}
    raise ChannelDeliveryError(f"WhatsApp's flow list for WABA {waba_id} runs past {_MAX_FLOW_LIST_PAGES} pages")


class FlowCreateResult(NamedTuple):
    """The create response the Flow lifecycle owner acts on.

    ``flow_id`` is the new draft's id; ``validation_errors`` is the vendor's
    top-level array — empty when the Flow JSON is valid, non-empty when Meta
    created the draft anyway but it can never publish. The lifecycle owner
    (``_resolve_flow_id``) decides whether to publish or delete.
    """

    flow_id: str
    validation_errors: list[dict[str, Any]]


async def create_flow(
    waba_id: str, name: str, flow_json: dict[str, Any], endpoint_uri: str | None = None
) -> FlowCreateResult:
    """Create a Flow under ``waba_id`` from ``flow_json``; return its create result.

    POSTs ``{name, categories: ["OTHER"], flow_json: <json string>}`` to
    ``{api}/{waba_id}/flows``. An ``endpoint_uri`` is included for an ENDPOINT-DRIVEN
    Flow, linking the published Flow to the plugin's data endpoint; a static Flow
    passes none. A 2xx that carries no ``id`` raises loudly (mirrors the no-message-id guard on
    the send path). A pure Graph wrapper: it returns both handles Meta returned (the draft id
    and any ``validation_errors``); the lifecycle owner acts on them.
    """
    url = f"{whatsapp_settings().api_base_url}/{waba_id}/flows"
    payload: dict[str, object] = {"name": name, "categories": ["OTHER"], "flow_json": json.dumps(flow_json)}
    if endpoint_uri is not None:
        payload["endpoint_uri"] = endpoint_uri
    response = await send_request(url, payload)
    body = response.json()
    flow_id = body.get("id")
    if not flow_id:
        raise ChannelDeliveryError("WhatsApp accepted the flow create but returned no flow id")
    return FlowCreateResult(flow_id=flow_id, validation_errors=body.get("validation_errors") or [])


async def set_business_public_key(phone_number_id: str, public_key_pem: str) -> None:
    """Register the business PUBLIC key for ``phone_number_id`` so Meta can encrypt data-endpoint payloads.

    POSTs ``{business_public_key: <PEM>}`` to ``{api}/{phone_number_id}/whatsapp_business_encryption``
    (the key the data endpoint's private key decrypts). Idempotent operator provisioning — run
    once per sending number, or after a key rotation. Raises ``ChannelDeliveryError`` on the
    ``send_request`` failure modes.
    """
    url = f"{whatsapp_settings().api_base_url}/{phone_number_id}/whatsapp_business_encryption"
    await send_request(url, {"business_public_key": public_key_pem})


async def publish_flow(flow_id: str) -> None:
    """Publish a draft Flow so it can be sent; POST ``{api}/{flow_id}/publish``."""
    url = f"{whatsapp_settings().api_base_url}/{flow_id}/publish"
    await send_request(url, {})


async def delete_flow(flow_id: str) -> None:
    """Delete a draft Flow; DELETE ``{api}/{flow_id}``.

    Cleans up a draft stranded when a publish or cache write fails after create, and
    a draft found on the WABA under a Flow's name before that Flow is created; Meta
    deletes only unpublished flows. Raises ``ChannelDeliveryError`` on failure like
    every builder; the caller decides what the failure means for its step.
    """
    url = f"{whatsapp_settings().api_base_url}/{flow_id}"
    await send_request(url, method="delete")
