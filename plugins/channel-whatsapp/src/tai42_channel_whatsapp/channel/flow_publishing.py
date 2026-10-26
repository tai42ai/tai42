"""Published-Flow resolution: one published Flow per Flow identity on a WABA.

A Flow's identity is the schema hash and the data-endpoint URI; it names the Flow
on Meta and keys the flow-id cache. A resolve answers from the cache, else finds
the live Flow on the WABA by name, else creates, publishes and caches a new one,
under a per-identity resolve lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_whatsapp.correlation import cache_flow_id, flow_resolve_lock, get_cached_flow_id
from tai42_channel_whatsapp.flow_assets import FlowSummary, create_flow, delete_flow, list_flows, publish_flow
from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

# A Flow's name on Meta is this prefix + the Flow identity (:func:`_flow_identity`).
# Meta enforces name uniqueness per WABA, and the name is the lookup key on the WABA
# when the flow-id cache has no entry. The identity covers the schema hash and the
# endpoint URI — everything the create sends that shapes the Flow — so a published
# (immutable) Flow's name vouches for its content.
_FLOW_NAME_PREFIX = "tai42-form-"

# Meta's Flow statuses, as Meta spells them. A live Flow is the plugin's published Flow
# (THROTTLED / BLOCKED only flag an unhealthy endpoint, which Meta's monitoring lifts);
# a DRAFT is editable, so its name does not vouch for its content; a DEPRECATED Flow can
# no longer be sent, deleted or restored.
_LIVE_FLOW_STATUSES = frozenset({"PUBLISHED", "THROTTLED", "BLOCKED"})
_DRAFT_FLOW_STATUS = "DRAFT"
_DEPRECATED_FLOW_STATUS = "DEPRECATED"

# The resolve lock's lifetime and a waiter's longest wait, as a multiple of the HTTP
# timeout setting. Nothing bounds the holder's own work to it: when the lock expires
# while the holder still works, a waiter may resolve too, and its create under the same
# name is refused by Meta as not unique and raises.
_RESOLVE_LOCK_HTTP_TIMEOUTS = 6


def _flow_identity(schema_hash: str, endpoint_uri: str | None) -> str:
    """sha256 hex over the schema hash and the endpoint URI: the published Flow's identity."""
    canonical = json.dumps(
        {"schema_hash": schema_hash, "endpoint_uri": endpoint_uri}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _find_named_flow(waba_id: str, name: str) -> str | None:
    """The live Flow already named ``name`` on the WABA, or ``None`` once the name is clear to create.

    A live match is returned for adoption. A DRAFT match is deleted (a failed delete
    raises: a create beside it would clash on the name). A DEPRECATED match is logged
    and left alone; the create's own answer then says whether Meta still reserves its
    name. Any other status, or more than one live match, raises.
    """
    matches = [flow for flow in await list_flows(waba_id) if flow.name == name]
    known = _LIVE_FLOW_STATUSES | {_DRAFT_FLOW_STATUS, _DEPRECATED_FLOW_STATUS}
    unknown = [flow for flow in matches if flow.status not in known]
    if unknown:
        raise ChannelDeliveryError(
            "WhatsApp lists "
            + ", ".join(f"flow {flow.flow_id} named {name} in unrecognised status {flow.status}" for flow in unknown)
        )
    live = [flow for flow in matches if flow.status in _LIVE_FLOW_STATUSES]
    if len(live) > 1:
        raise ChannelDeliveryError(
            f"WhatsApp lists {len(live)} live flows named {name}: {_flow_ids(live)}; a flow name is unique per WABA"
        )
    if live:
        return live[0].flow_id
    for draft in (flow for flow in matches if flow.status == _DRAFT_FLOW_STATUS):
        await delete_flow(draft.flow_id)
    deprecated = [flow for flow in matches if flow.status == _DEPRECATED_FLOW_STATUS]
    if deprecated:
        logger.warning(
            "deprecated flow(s) %s already carry name %s on WABA %s; creating a new flow",
            _flow_ids(deprecated),
            name,
            waba_id,
        )
    return None


def _flow_ids(flows: list[FlowSummary]) -> str:
    return ", ".join(flow.flow_id for flow in flows)


async def _resolve_flow_id(
    waba_id: str, schema_hash: str, flow_json: dict[str, Any], endpoint_uri: str | None = None
) -> str:
    """The published flow id for this Flow under ``waba_id``.

    The Flow's identity is :func:`_flow_identity` of ``schema_hash`` and ``endpoint_uri``;
    it names the Flow on Meta and keys the flow-id cache. The cached id, else — under the
    resolve lock, re-reading the cache once held — the live Flow found on the WABA by name
    (see :func:`_find_named_flow`), else create + publish + store a new Flow.
    ``endpoint_uri`` links an endpoint-driven Flow to the plugin's data
    endpoint at create; a static Flow passes none. Every step is loud — a list, delete,
    create, publish, or store failure raises and never falls back to another answer format.

    Meta returns HTTP 200 for a create even when the Flow JSON is invalid: the draft
    IS created but carries ``validation_errors`` and can never publish. A non-empty
    list is refused loudly with the full list rendered, BEFORE ``publish_flow``.

    That refusal — and a publish or cache failure AFTER a successful create — strands
    the draft on Meta, so the draft is best-effort deleted before re-raising the
    original error; a delete that itself fails is logged without masking it, and the
    next resolve finds that draft by name and deletes it. This is the ONE writer of a
    Flow's lifecycle.
    """
    identity = _flow_identity(schema_hash, endpoint_uri)
    cached = await get_cached_flow_id(waba_id, identity)
    if cached is not None:
        return cached
    ttl_seconds = _RESOLVE_LOCK_HTTP_TIMEOUTS * whatsapp_settings().http_timeout_seconds
    async with flow_resolve_lock(waba_id, identity, ttl_seconds=ttl_seconds):
        cached = await get_cached_flow_id(waba_id, identity)
        if cached is not None:
            return cached
        name = f"{_FLOW_NAME_PREFIX}{identity}"
        found = await _find_named_flow(waba_id, name)
        if found is not None:
            await cache_flow_id(waba_id, identity, found)
            return found
        result = await create_flow(waba_id=waba_id, name=name, flow_json=flow_json, endpoint_uri=endpoint_uri)
        try:
            if result.validation_errors:
                # The refusal rides the SAME except that deletes an orphaned draft, so the
                # invalid draft is cleaned up by the one lifecycle writer — hence raising here.
                raise ChannelDeliveryError(  # noqa: TRY301
                    "WhatsApp created the flow draft with validation errors and it can never publish: "
                    + json.dumps(result.validation_errors, sort_keys=True, separators=(",", ":"))[:1000]
                )
            await publish_flow(result.flow_id)
            await cache_flow_id(waba_id, identity, result.flow_id)
        except Exception:
            try:
                await delete_flow(result.flow_id)
            except Exception:
                logger.exception("failed to delete orphaned draft flow %s after resolve failure", result.flow_id)
            raise
        return result.flow_id
