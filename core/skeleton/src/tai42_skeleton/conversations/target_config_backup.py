"""The ``conversation_target_config`` backup section — export/import over the per-target config store.

The store carries the ``multichannel`` opt-in + first-contact ``greeting_template``.

Operator config, carrying no credentials, so the section is not secret-flagged — the same
split the connectors subsystem draws between its non-secret ``connector_categories`` and its
secret ``connector_connections``. Each restored row is re-validated by the model (the
``{pairing_code}``-only placeholder rule and the non-blank-template rule) and written through
the shared write service, so a malformed row or a refused binding is a per-row rejection, never
an aborted restore of the rest.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import ValidationError
from tai42_contract.backup import BackupSectionReport
from tai42_contract.conversations import TargetConversationConfig
from tai42_contract.states.errors import StatesError

from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore, write_target_config
from tai42_skeleton.tools.state_binding import is_binding_refusal

logger = logging.getLogger(__name__)


def _empty_report() -> BackupSectionReport:
    # ``skipped_existing`` (configs left untouched under ``skip``) is this section's own
    # count, so it rides ``details``.
    return BackupSectionReport(details={"skipped_existing": 0})


async def export_target_configs() -> dict[str, Any]:
    """The stored per-target configs.

    An in-memory deployment provably holds none, so it exports empty rather than refusing.
    """
    manager = get_conversations_manager()
    if not manager.durable:
        return {"target_configs": []}
    configs, _ = await manager.target_configs.list()
    return {"target_configs": [config.model_dump(mode="json") for config in configs.values()]}


async def _write_row(
    config: TargetConversationConfig,
    store: ConversationTargetConfigStore,
    key: Any,
    existing: set[tuple[str, str]],
    report: BackupSectionReport,
) -> None:
    """Write ``config`` through the write service and count it, or record its per-row rejection.

    A binding the states store refuses, or a ``ValueError`` the write raises, is the row's own
    defect; any other error (an unbound store, a store or transport fault) fails the section.
    """
    try:
        await write_target_config(config, store=store)
    except ValueError as exc:
        report.errors.append(f"config {key!r}: {exc}")
        report.skipped += 1
        return
    except StatesError as exc:
        if not is_binding_refusal(exc):
            raise
        report.errors.append(f"config {key!r}: {exc}")
        report.skipped += 1
        return
    pair = (config.target_kind, config.target_name)
    if pair in existing:
        report.updated += 1
    else:
        report.created += 1
    # Now that this pair is stored, a later duplicate in the same payload must treat it as
    # existing: skipped_existing under skip, updated under overwrite.
    existing.add(pair)


async def import_target_configs(
    payload: dict[str, Any], mode: Literal["skip", "overwrite"] = "skip"
) -> BackupSectionReport:
    """Restore per-target configs, keyed by ``(target_kind, target_name)``.

    A malformed envelope raises BEFORE any write. Each row is model-validated and written
    through the write service the set door uses (a carried binding is validated and its
    templates attached); a row failing validation or carrying a refused binding is a per-row
    rejection in the report, while a store or transport failure fails the section. The target's
    existence is not checked: a config may name a target a later section restores or a later
    registration provides. Under ``skip`` (the default) an existing config is left untouched;
    under ``overwrite`` it is replaced.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"conversation_target_config section payload must be an envelope dict, got {type(payload)}")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    if "target_configs" not in payload:
        raise ValueError("conversation_target_config envelope is missing the required 'target_configs' key")
    if not isinstance(payload["target_configs"], list):
        raise ValueError("conversation_target_config envelope 'target_configs' must be a list")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour

    report = _empty_report()
    if not payload["target_configs"]:
        # Nothing to write: a no-op on every deployment.
        return report

    manager = get_conversations_manager()
    if not manager.durable:
        # The store cannot hold a row on a backend-less deployment; refuse the whole section
        # loudly rather than silently drop every config.
        raise RuntimeError("conversation target config requires the redis conversations backend to restore")

    store = manager.target_configs
    # A MUTABLE snapshot of the stored pairs: a row written earlier IN THIS payload is added
    # below, so a later duplicate of the same pair is seen as existing rather than treated as
    # a second fresh create silently overwriting the first.
    stored, _ = await store.list()
    existing = set(stored)

    for item in payload["target_configs"]:
        key = (item.get("target_kind"), item.get("target_name")) if isinstance(item, dict) else None
        try:
            config = TargetConversationConfig.model_validate(item)
        except ValidationError as exc:
            # Rejected per row rather than written unvalidated.
            report.errors.append(f"config {key!r}: {exc}")
            report.skipped += 1
            continue
        pair = (config.target_kind, config.target_name)
        if pair in existing and mode == "skip":
            report.details["skipped_existing"] += 1
            continue
        await _write_row(config, store, key, existing, report)

    return report
