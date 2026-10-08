"""The ``conversations`` backup section — export/import over the routing-row store.

Only the routing rows are backed up; the record/dedupe/reverse-index keyspaces are transient.

``callback_secret`` is EXCLUDED from the export — a live secret never leaves the host.
Under ``overwrite`` a row is replaced and its ``api`` secret re-minted (surfaced in
``new_callback_secrets``); under ``skip`` (the default) an existing route is left FULLY
untouched — no re-mint — so a re-import never invalidates a live signer.

``execution_key_fingerprint`` IS exported: import asserts the live key still carries it and
is token-free-evaluable, refusing per row a key revoked+reminted since the backup.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import ValidationError
from tai42_contract.backup import BackupSectionReport
from tai42_contract.conversations import ConversationRoute
from tai42_contract.states.errors import StatesError

from tai42_skeleton.authz.execution import ExecutionKeyAuthorityError, ExecutionKeyScan
from tai42_skeleton.authz.token_free import TokenFreeConditionError
from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.managers.base_conversations_manager import (
    BaseConversationsManager,
    DoorFlipRefusedError,
)
from tai42_skeleton.conversations.route_writes import RouteBindRefusedError, write_route
from tai42_skeleton.tools.state_binding import is_binding_refusal

logger = logging.getLogger(__name__)


def _empty_report() -> BackupSectionReport:
    # ``skipped_existing`` (routes left untouched under ``skip``) and the once-shown
    # ``new_callback_secrets`` are this section's own counts, so they ride ``details``.
    return BackupSectionReport(details={"skipped_existing": 0, "new_callback_secrets": []})


async def export_conversation_routes() -> dict[str, Any]:
    """The stored routing rows, each ``callback_secret`` EXCLUDED.

    An in-memory deployment provably holds no rows, so it exports empty rather than refusing.
    """
    manager = get_conversations_manager()
    if not manager.durable:
        return {"routes": []}
    routes, _ = await manager.list_routes()
    exported: list[dict[str, Any]] = []
    for route in routes.values():
        data = route.model_dump(mode="json")
        # A live secret never leaves the host — import re-mints a fresh one per row.
        data.pop("callback_secret", None)
        exported.append(data)
    return {"routes": exported}


def _validate_row(
    item: Any, existing: dict[str, ConversationRoute], mode: Literal["skip", "overwrite"], report: BackupSectionReport
) -> ConversationRoute | None:
    """The :class:`ConversationRoute` a backup row parses to, or ``None`` when it is not written.

    ``None`` when the row is rejected (a validation failure recorded per row) or left fully
    untouched (an existing route under ``skip`` — no re-mint, no re-assertion of its execution
    key).
    """
    route_name = item.get("route_name") if isinstance(item, dict) else None
    try:
        route = ConversationRoute.model_validate(item)
    except ValidationError as exc:
        # Rejected per row rather than written unanchored.
        report.errors.append(f"route {route_name!r}: {exc}")
        report.skipped += 1
        return None
    if route.route_name in existing and mode == "skip":
        report.details["skipped_existing"] += 1
        return None
    return route


async def _authorize_row(route: ConversationRoute, scan: ExecutionKeyScan, report: BackupSectionReport) -> bool:
    """Whether ``route``'s execution key asserts usable and token-free-evaluable, a per-row rejection otherwise."""
    try:
        # The same assertion the create door makes; a key reminted since the backup no
        # longer carries the row's bound fingerprint.
        await scan.assert_usable(route.execution_key, bound_fingerprint=route.execution_key_fingerprint)
    except (ExecutionKeyAuthorityError, TokenFreeConditionError) as exc:
        # A property of the ROW, so it is rejected per row. Other types (a corrupt stored
        # policy, a store read error) propagate as the section's own failure.
        report.errors.append(f"route {route.route_name!r}: {exc}")
        report.skipped += 1
        return False
    return True


async def _write_row(
    route: ConversationRoute,
    manager: BaseConversationsManager,
    existing: dict[str, ConversationRoute],
    report: BackupSectionReport,
) -> None:
    """Write ``route`` through the route write service, recording it or its per-row refusal.

    The write runs the route's save rules (the bind check, the expressions, the canonical
    ``(channel, our_identity)`` claim) and mints a fresh callback secret (shown once); export
    carried no secret. A refusal of the route itself is a per-row rejection in the report; any
    other failure (a store or transport fault) propagates as the section's failure.
    created/updated follows the pre-restore snapshot, not the store's return.
    """
    try:
        result = await write_route(route.model_copy(update={"callback_secret": None}), manager=manager)
    except RouteBindRefusedError as exc:
        report.errors.append(f"route {route.route_name!r}: {'; '.join(exc.lines)}")
        report.skipped += 1
        return
    except (ValueError, DoorFlipRefusedError) as exc:
        report.errors.append(f"route {route.route_name!r}: {exc}")
        report.skipped += 1
        return
    except StatesError as exc:
        if not is_binding_refusal(exc):
            raise
        report.errors.append(f"route {route.route_name!r}: {exc}")
        report.skipped += 1
        return
    if route.route_name in existing:
        report.updated += 1
    else:
        report.created += 1
    if result.callback_secret is not None:
        report.details["new_callback_secrets"].append(
            {"route_name": route.route_name, "callback_secret": result.callback_secret}
        )


async def import_conversation_routes(
    payload: dict[str, Any], mode: Literal["skip", "overwrite"] = "skip"
) -> BackupSectionReport:
    """Restore routing rows, keyed by ``route_name``.

    A malformed envelope raises BEFORE any write. Each row written is validated, its
    execution key asserted usable and token-free-evaluable against the LIVE policy store
    (pass-role skipped — the restore door is admin-fenced), then written through the route
    write service the create door uses (the bind check, the expressions, the canonical
    ``(channel, our_identity)`` claim, the store's door-flip refusal). A row failing any of these
    is a per-row rejection in the report, never an aborted restore of the rest; a store or
    transport failure fails the section. The target's existence is not checked: a route may name
    a target a later section restores or a later registration provides.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"conversations section payload must be an envelope dict, got {type(payload)}")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    if "routes" not in payload:
        raise ValueError("conversations envelope is missing the required 'routes' key")
    if not isinstance(payload["routes"], list):
        raise ValueError("conversations envelope 'routes' must be a list")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour

    report = _empty_report()
    if not payload["routes"]:
        # Nothing to write: a no-op on every deployment.
        return report

    manager = get_conversations_manager()
    if not manager.durable:
        # The store cannot hold a row on a backend-less deployment; refuse the whole
        # section loudly rather than silently drop every route.
        raise RuntimeError("conversation routes require the redis conversations backend to restore")

    existing, _ = await manager.list_routes()
    # ONE scan for the whole restore, so each distinct key is read and rendered once.
    scan = ExecutionKeyScan()

    for item in payload["routes"]:
        route = _validate_row(item, existing, mode, report)
        if route is None:
            continue
        if not await _authorize_row(route, scan, report):
            continue
        await _write_row(route, manager, existing, report)

    return report
