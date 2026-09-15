"""The webhooks backup section: export, import, and the per-phase restore helpers.

The one non-thin backup section — the export gathers hooks, per-topic verifier
bindings, and trigger-link records (hashes and metadata only, never a raw token);
the import validates the envelope, replays the ingress locks BEFORE the records they
gate, and restores hooks, tombstones, and trigger links, refusing any record on a
topic whose verifier binding failed.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from tai42_contract.hooks import HookParams

from tai42_skeleton.authz.execution import ExecutionKeyAuthorityError, ExecutionKeyScan
from tai42_skeleton.authz.token_free import TokenFreeConditionError
from tai42_skeleton.backup.registry import current_import_mode
from tai42_skeleton.backup.sections import _empty_report
from tai42_skeleton.hooks import cache as hooks_cache
from tai42_skeleton.hooks.trigger_links import (
    TriggerLinkError,
    bound_hashes_by_name,
    export_trigger_links,
    restore_tombstone,
    restore_trigger_link,
)


async def _export_webhooks() -> dict[str, Any]:
    manager = hooks_cache.get_hooks_manager()
    hooks = await manager.list_hooks()
    # Envelope carries hooks + per-topic verifier bindings + trigger-link records (hashes
    # and metadata only, never a raw token). Bindings must travel with the hooks, or a
    # verified topic restores as a public door.
    return {
        "hooks": [params.model_dump(mode="json") for params in hooks.values()],
        "topic_verifiers": await manager.all_topic_verifiers(),
        **(await export_trigger_links()),
    }


def _validate_webhooks_envelope(payload: dict[str, Any]) -> tuple[list, dict, list, list]:
    """Reject a malformed webhooks envelope loudly BEFORE any write.

    Returns the ``(hooks, topic_verifiers, trigger_links, tombstones)`` the restore replays. A
    missing key or wrong-typed value raises rather than defaulting an empty section.
    """
    for key in ("hooks", "trigger_links", "tombstones"):
        if key not in payload:
            raise ValueError(f"webhooks envelope is missing the required {key!r} key")
        if not isinstance(payload[key], list):
            raise ValueError(f"webhooks envelope {key!r} must be a list")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    if "topic_verifiers" not in payload:
        raise ValueError("webhooks envelope is missing the required 'topic_verifiers' key")
    if not isinstance(payload["topic_verifiers"], dict):
        raise ValueError("webhooks envelope 'topic_verifiers' must be a mapping of topic to binding")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    return payload["hooks"], payload["topic_verifiers"], payload["trigger_links"], payload["tombstones"]


async def _restore_hooks(
    manager: Any,
    mode: str,
    report: dict[str, Any],
    scan: ExecutionKeyScan,
    hooks: list[dict[str, Any]],
    unlocked_topics: frozenset[str] = frozenset(),
) -> None:
    """Restore hook records, each rejection a per-hook error.

    Per hook: validate / skip-existing / unlocked-topic refuse / execution-key assert / register. Never a hook
    without a bounded identity nor an aborted restore.
    """
    existing = await manager.list_hooks()
    for item in hooks:
        name = item.get("name") if isinstance(item, dict) else None
        try:
            params = HookParams.model_validate(item)
        except ValidationError as exc:
            report["errors"].append(f"hook {name!r}: {exc}")
            report["skipped"] += 1
            continue
        if params.name in existing and mode == "skip":
            # Existing hook (keyed by name) left untouched — not re-registered, not re-validated.
            report["skipped_existing"] += 1
            continue
        if params.topic in unlocked_topics:
            # Lock absent: writing the hook would hand an unverified public door its execution key.
            report["errors"].append(
                f"hook {name!r}: topic {params.topic!r} is not restored — its verifier binding failed, "
                "which would leave this hook live on an unverified public door"
            )
            report["skipped"] += 1
            continue
        try:
            # A stored hook must name a usable, token-free-evaluable execution key,
            # asserted exactly as the register door does (pass-role half not asserted:
            # this route is admin-only fenced).
            await scan.assert_usable(params.execution_key, bound_fingerprint=params.execution_key_fingerprint)
        except (ExecutionKeyAuthorityError, TokenFreeConditionError) as exc:
            # Only these two are per-record faults; other types propagate as the section's failure.
            report["errors"].append(f"hook {name!r}: {exc}")
            report["skipped"] += 1
            continue
        try:
            await manager.register(params)
        except ValueError as exc:
            # A non-compiling inline condition/expr jq is a per-hook rejection; other
            # types (store/transport) propagate as the section's failure.
            report["errors"].append(f"hook {name!r}: {exc}")
            report["skipped"] += 1
            continue
        if params.name in existing:
            report["updated"] += 1
        else:
            report["created"] += 1


async def _restore_topic_verifiers(
    manager: Any,
    mode: str,
    report: dict[str, Any],
    topic_verifiers: dict[str, Any],
    existing_verifiers: dict[str, Any],
) -> set[str]:
    """Replay the ingress locks, reporting which failed.

    Returns the unlocked-topic set — every record ON such a topic is refused by the callers, so no window exists
    in which a restored hook is reachable through a door the backup had verified.
    """
    unlocked_topics: set[str] = set()
    for topic, binding in topic_verifiers.items():
        if not isinstance(topic, str) or not topic:
            report["errors"].append(f"topic verifier with missing or empty topic: {topic!r}")
            report["skipped"] += 1
            continue
        if topic in existing_verifiers and mode == "skip":
            # Existing verifier binding left in place; the lock is present, so records on
            # this topic are NOT treated as unlocked below.
            report["skipped_existing"] += 1
            continue
        try:
            # Validates the binding shape on write: a bad entry is a per-topic rejection.
            await manager.set_topic_verifier(topic, binding)
        except ValidationError as exc:
            report["errors"].append(f"topic verifier {topic!r}: {exc}")
            report["skipped"] += 1
            unlocked_topics.add(topic)
            continue
        if topic in existing_verifiers:
            report["updated"] += 1
        else:
            report["created"] += 1
    return unlocked_topics


async def _restore_tombstones(report: dict[str, Any], tombstones: list[Any]) -> None:
    """Restore tombstones first, so a tombstoned hash then refuses its own record below (tombstone wins).

    An idempotent set-union keyed by ``token_hash``.
    """
    for token_hash in tombstones:
        try:
            await restore_tombstone(token_hash)
        except TriggerLinkError as exc:
            report["errors"].append(f"tombstone {token_hash!r}: {exc.message}")
            report["skipped"] += 1


async def _restore_trigger_links(
    report: dict[str, Any],
    mode: str,
    trigger_links: list[Any],
    live_link_names: dict[str, str],
    unlocked_topics: set[str],
    scan: ExecutionKeyScan,
) -> None:
    """Restore each trigger link, reporting per-item outcomes.

    A link on an unlocked topic is refused (it would go back on an unverified public door).
    """
    for item in trigger_links:
        name = item.get("name") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict):
                raise TriggerLinkError(400, "trigger link entry must be a JSON object")  # noqa: TRY301 raised to the shared per-item handler below that records it as a per-link error and continues
            if item["name"] in live_link_names and mode == "skip":
                # Existing trigger link (keyed by name) left untouched — its live record
                # and token hash stand, so a re-import does not re-key it.
                report["skipped_existing"] += 1
                continue
            record = item["record"]
            topic = record.get("topic") if isinstance(record, dict) else None
            if topic in unlocked_topics:
                # Lock absent: restoring the link would put it back on a verified door.
                raise TriggerLinkError(  # noqa: TRY301 raised to the shared per-item handler below that records it as a per-link error and continues
                    400,
                    f"topic {topic!r} is not restored — its verifier binding failed, which would take this "
                    "link back into service on an unverified public door",
                )
            outcome = await restore_trigger_link(
                name=item["name"], token_hash=item["token_hash"], record=record, scan=scan
            )
        except (TriggerLinkError, KeyError) as exc:
            message = exc.message if isinstance(exc, TriggerLinkError) else f"missing key {exc}"
            report["errors"].append(f"trigger link {name!r}: {message}")
            report["skipped"] += 1
            continue
        if outcome in ("skipped_expired", "skipped_tombstoned"):
            report["skipped"] += 1
        elif outcome == "updated":
            report["updated"] += 1
        else:
            report["created"] += 1


async def _import_webhooks(payload: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    """Order the webhooks restore.

    A bare LIST is the hooks-only shape; else validate the envelope, run the duplicate-hash pre-scan,
    replay topic verifiers, restore hooks, tombstones, and trigger links.
    """
    manager = hooks_cache.get_hooks_manager()
    mode = current_import_mode()
    report = _empty_report()
    # One scan for the whole restore: each distinct execution key is read and rendered once.
    scan = ExecutionKeyScan()

    # A bare LIST is the hooks-only backup shape (no trigger-link envelope).
    if isinstance(payload, list):
        await _restore_hooks(manager, mode, report, scan, payload)
        return report

    if not isinstance(payload, dict):
        raise ValueError(  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
            f"webhooks section payload must be a list (old shape) or an envelope dict, got {type(payload)}"
        )
    hooks, topic_verifiers, trigger_links, tombstones = _validate_webhooks_envelope(payload)

    # Whole-section pre-write scan, touching zero keys: refuse a payload binding ONE
    # hash under TWO names, internally or against the LIVE index (a later revoke treats
    # an orphan binding as authoritative and would destroy the new name's record). A
    # NAME appearing twice is fine — it resolves last-wins through displacement.
    live_link_names = await bound_hashes_by_name()
    _reject_duplicate_hash_binding(trigger_links, live_link_names)

    # Ingress locks go back BEFORE the records they gate.
    existing_verifiers = await manager.all_topic_verifiers()
    unlocked_topics = await _restore_topic_verifiers(manager, mode, report, topic_verifiers, existing_verifiers)

    await _restore_hooks(manager, mode, report, scan, hooks, frozenset(unlocked_topics))
    await _restore_tombstones(report, tombstones)
    await _restore_trigger_links(report, mode, trigger_links, live_link_names, unlocked_topics, scan)
    return report


def _reject_duplicate_hash_binding(trigger_links: Any, live_by_name: dict[str, str]) -> None:
    """Raise if a token hash is bound under two DIFFERENT names, in the payload or against the live store index.

    Zero keys written. ``live_by_name`` maps
    every ``name:*`` store binding (orphans included) to its hash, so binding a new name
    to an already-orphaned hash is refused too (a later revoke would destroy the new
    name's live record).
    """
    # Malformed entries are left to the per-item restore to reject loudly.
    hash_to_name: dict[str, str] = {}
    for item in trigger_links:
        if not isinstance(item, dict):
            continue
        name, token_hash = item.get("name"), item.get("token_hash")
        if not isinstance(name, str) or not isinstance(token_hash, str):
            continue
        prior = hash_to_name.get(token_hash)
        if prior is not None and prior != name:
            raise ValueError(
                f"webhooks envelope binds token hash {token_hash!r} under two names ({prior!r} and {name!r})"
            )
        hash_to_name[token_hash] = name

    live_by_hash = {token_hash: name for name, token_hash in live_by_name.items()}
    for token_hash, name in hash_to_name.items():
        live_name = live_by_hash.get(token_hash)
        if live_name is not None and live_name != name:
            raise ValueError(
                f"import binds token hash {token_hash!r} to {name!r} but it is already live under {live_name!r}"
            )
