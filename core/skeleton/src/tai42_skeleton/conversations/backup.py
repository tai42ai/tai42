"""The ``conversations`` backup section — export/import over the routing-row store. Only
the routing rows are backed up; the record/dedupe/reverse-index keyspaces are transient.

``callback_secret`` is EXCLUDED from the export — a live secret never leaves the host.
Under ``overwrite`` a row is replaced and its ``api`` secret re-minted (surfaced in
``new_callback_secrets``); under ``skip`` (the default) an existing route is left FULLY
untouched — no re-mint — so a re-import never invalidates a live signer.

``execution_key_fingerprint`` IS exported: import asserts the live key still carries it and
is token-free-evaluable, refusing per row a key revoked+reminted since the backup.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Literal

from pydantic import ValidationError
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.authz.execution import ExecutionKeyAuthorityError, ExecutionKeyScan
from tai42_skeleton.authz.token_free import TokenFreeConditionError
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager

logger = logging.getLogger(__name__)

_SectionReport = dict[str, Any]


def _empty_report() -> _SectionReport:
    return {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": [], "new_callback_secrets": []}


def _channel_identity(route: ConversationRoute) -> tuple[str, str] | None:
    """The ``(channel, canonical identity)`` pair a ``channel`` row claims; ``None`` for an
    ``api`` row, which claims none."""
    if route.door != "channel" or route.channel is None or route.our_identity is None:
        return None
    return (route.channel, canonical_address(route.our_identity))


async def export_conversation_routes() -> dict[str, Any]:
    """The stored routing rows, each ``callback_secret`` EXCLUDED. An in-memory deployment
    provably holds no rows, so it exports empty rather than refusing."""
    manager = get_conversations_manager()
    if isinstance(manager, InMemoryConversationsManager):
        return {"routes": []}
    routes = await manager.list_routes()
    exported: list[dict[str, Any]] = []
    for route in routes.values():
        data = route.model_dump(mode="json")
        # A live secret never leaves the host — import re-mints a fresh one per row.
        data.pop("callback_secret", None)
        exported.append(data)
    return {"routes": exported}


async def import_conversation_routes(
    payload: dict[str, Any], mode: Literal["skip", "overwrite"] = "skip"
) -> _SectionReport:
    """Restore routing rows, keyed by ``route_name``.

    A malformed envelope raises BEFORE any write. Each row written is validated, its
    execution key asserted usable and token-free-evaluable against the LIVE policy store
    (pass-role skipped — the restore door is admin-fenced), and its
    ``(channel, our_identity)`` claim checked unclaimed. A row failing any of these is a
    per-row rejection in the report, never an aborted restore of the rest."""
    if not isinstance(payload, dict):
        raise ValueError(f"conversations section payload must be an envelope dict, got {type(payload)}")
    if "routes" not in payload:
        raise ValueError("conversations envelope is missing the required 'routes' key")
    if not isinstance(payload["routes"], list):
        raise ValueError("conversations envelope 'routes' must be a list")

    report = _empty_report()
    if not payload["routes"]:
        # Nothing to write: a no-op on every deployment.
        return report

    manager = get_conversations_manager()
    if isinstance(manager, InMemoryConversationsManager):
        # The store cannot hold a row on a backend-less deployment; refuse the whole
        # section loudly rather than silently drop every route.
        raise RuntimeError("conversation routes require the redis conversations backend to restore")

    existing = await manager.list_routes()
    # Live ``(channel, identity)`` claims, tracked across the restore: two channel rows on
    # one identity are unresolvable, refused here as the create door refuses them.
    claimed = {pair: row.route_name for row in existing.values() if (pair := _channel_identity(row)) is not None}
    # ONE scan for the whole restore, so each distinct key is read and rendered once.
    scan = ExecutionKeyScan()

    for item in payload["routes"]:
        route_name = item.get("route_name") if isinstance(item, dict) else None
        try:
            route = ConversationRoute.model_validate(item)
        except ValidationError as exc:
            # Rejected per row rather than written unanchored.
            report["errors"].append(f"route {route_name!r}: {exc}")
            report["skipped"] += 1
            continue
        if route.route_name in existing and mode == "skip":
            # Left fully untouched — no re-mint, no re-assertion of its execution key.
            report["skipped_existing"] += 1
            continue
        try:
            # The same assertion the create door makes; a key reminted since the backup no
            # longer carries the row's bound fingerprint.
            await scan.assert_usable(route.execution_key, bound_fingerprint=route.execution_key_fingerprint)
        except (ExecutionKeyAuthorityError, TokenFreeConditionError) as exc:
            # A property of the ROW, so it is rejected per row. Other types (a corrupt
            # stored policy, a store read error) propagate as the section's own failure.
            report["errors"].append(f"route {route.route_name!r}: {exc}")
            report["skipped"] += 1
            continue

        pair = _channel_identity(route)
        holder = claimed.get(pair) if pair is not None else None
        if pair is not None and holder is not None and holder != route.route_name:
            report["errors"].append(
                f"route {route.route_name!r}: channel {pair[0]!r} identity {pair[1]!r} is already routed by {holder!r}"
            )
            report["skipped"] += 1
            continue

        # Export carried no secret; mint one here and show it once. A ``channel`` row
        # signs nothing and carries none.
        callback_secret = secrets.token_urlsafe(32) if route.door == "api" else None
        restored = route.model_copy(update={"callback_secret": callback_secret})

        # created/updated follows the pre-restore snapshot, not the store's return.
        await manager.put_route(restored)
        # The row may have moved off the pair it held before this write.
        for held in [held for held, owner in claimed.items() if owner == route.route_name]:
            claimed.pop(held)
        if pair is not None:
            claimed[pair] = route.route_name
        if route.route_name in existing:
            report["updated"] += 1
        else:
            report["created"] += 1
        if callback_secret is not None:
            report["new_callback_secrets"].append({"route_name": route.route_name, "callback_secret": callback_secret})

    return report
