"""The platform state-module SEED applier — the module-side twin of the preset applier.

Shipped default modules are declared through the facet (``tai42_app.states.
register_module_seed``). At startup :func:`apply_module_seeds` writes each shipped default
that is absent from the store, stamping its canonical body hash as ``shipped_hash`` — the
``shipped_default`` discriminator the module catalog read exposes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable

from tai42_contract.states.models import StateModuleDocument

from tai42_skeleton.states.store import PostgresStatesStore

logger = logging.getLogger(__name__)


class StateModuleSeedRegistry:
    """The process-wide shipped-module-seed registry — the body behind
    ``app.states.register_module_seed``. Declaring two seeds under one name raises loudly.
    Reset each ``start()`` so a reload re-registers cleanly."""

    def __init__(self) -> None:
        self._seeds: dict[str, StateModuleDocument] = {}

    def register(self, doc: StateModuleDocument) -> None:
        if doc.name in self._seeds:
            raise ValueError(f"state-module seed {doc.name!r} is already registered")
        self._seeds[doc.name] = doc

    def seeds(self) -> list[StateModuleDocument]:
        return list(self._seeds.values())

    def reset(self) -> None:
        self._seeds.clear()


def _canonical_hash(body: dict) -> str:
    """A byte-stable content hash of a module body, stamped as its ``shipped_hash``."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def apply_module_seeds(store: PostgresStatesStore, *, seeds: Iterable[StateModuleDocument]) -> None:
    """Write each shipped default module that is absent from the store, stamping its
    canonical body hash as ``shipped_hash``. Idempotent — a present name is left untouched."""
    for doc in seeds:
        if await store.get_module(doc.name) is not None:
            continue
        body = doc.model_dump(by_alias=True)
        await store.upsert_module(doc.name, body, _canonical_hash(body))
        logger.info("state-module seed %r: created", doc.name)


__all__ = ["StateModuleSeedRegistry", "apply_module_seeds"]
