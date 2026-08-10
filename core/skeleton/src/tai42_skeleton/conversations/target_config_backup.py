"""The ``conversation_target_config`` backup section — export/import over the per-target
config store (``multichannel`` opt-in + first-contact ``greeting_template``).

Operator config, carrying no credentials, so the section is not secret-flagged — the same
split the connectors subsystem draws between its non-secret ``connector_categories`` and its
secret ``connector_connections``. Each restored row is re-validated by the model (the
``{pairing_code}``-only placeholder rule and the non-blank-template rule), so a malformed
row is a per-row rejection, never an aborted restore of the rest.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import ValidationError
from tai42_contract.conversations import TargetConversationConfig

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore

logger = logging.getLogger(__name__)

_SectionReport = dict[str, Any]


def _empty_report() -> _SectionReport:
    return {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}


async def export_target_configs() -> dict[str, Any]:
    """The stored per-target configs. An in-memory deployment provably holds none, so it
    exports empty rather than refusing."""
    if ConversationsSettings().in_memory:
        return {"target_configs": []}
    configs = await ConversationTargetConfigStore(ConversationsSettings()).list()
    return {"target_configs": [config.model_dump(mode="json") for config in configs.values()]}


async def import_target_configs(payload: dict[str, Any], mode: Literal["skip", "overwrite"] = "skip") -> _SectionReport:
    """Restore per-target configs, keyed by ``(target_kind, target_name)``.

    A malformed envelope raises BEFORE any write. Each row is model-validated; a row failing
    validation is a per-row rejection in the report. Under ``skip`` (the default) an existing
    config is left untouched; under ``overwrite`` it is replaced."""
    if not isinstance(payload, dict):
        raise ValueError(f"conversation_target_config section payload must be an envelope dict, got {type(payload)}")
    if "target_configs" not in payload:
        raise ValueError("conversation_target_config envelope is missing the required 'target_configs' key")
    if not isinstance(payload["target_configs"], list):
        raise ValueError("conversation_target_config envelope 'target_configs' must be a list")

    report = _empty_report()
    if not payload["target_configs"]:
        # Nothing to write: a no-op on every deployment.
        return report

    if ConversationsSettings().in_memory:
        # The store cannot hold a row on a backend-less deployment; refuse the whole section
        # loudly rather than silently drop every config.
        raise RuntimeError("conversation target config requires the redis conversations backend to restore")

    store = ConversationTargetConfigStore(ConversationsSettings())
    # A MUTABLE snapshot of the stored pairs: a row written earlier IN THIS payload is added
    # below, so a later duplicate of the same pair is seen as existing rather than treated as
    # a second fresh create silently overwriting the first.
    existing = set(await store.list())

    for item in payload["target_configs"]:
        key = (item.get("target_kind"), item.get("target_name")) if isinstance(item, dict) else None
        try:
            config = TargetConversationConfig.model_validate(item)
        except ValidationError as exc:
            # Rejected per row rather than written unvalidated.
            report["errors"].append(f"config {key!r}: {exc}")
            report["skipped"] += 1
            continue
        pair = (config.target_kind, config.target_name)
        if pair in existing and mode == "skip":
            report["skipped_existing"] += 1
            continue
        await store.upsert(config)
        if pair in existing:
            report["updated"] += 1
        else:
            report["created"] += 1
        # Now that this pair is stored, a later duplicate in the same payload must treat it as
        # existing: skipped_existing under skip, updated under overwrite.
        existing.add(pair)

    return report
