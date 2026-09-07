"""The platform state-module SEED applier — the module-side twin of the preset applier.

Shipped default modules are declared through the facet (``tai42_app.states.
register_module_seed``) and retired names through ``register_retired_module_name``. At
startup :func:`apply_module_seeds` reconciles the store against them, with four semantics
keyed on the ``shipped_hash`` column:

- CREATE when absent — write the body and stamp its canonical hash as ``shipped_hash``;
- UPGRADE on drift — a stored module still equal to its ``shipped_hash`` (unedited) whose
  shipped body changed is overwritten and re-stamped;
- SKIP operator-edited — a stored body whose hash no longer matches ``shipped_hash`` (an
  operator upload sets it NULL; an edit changes the hash) is left untouched, logged;
- DELETE retired — a retired name still unedited is deleted (kept and logged when edited,
  or when it is still mounted — a mounted module can never be silently dropped).
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
    ``app.states.register_module_seed`` / ``register_retired_module_name``. Declaring two
    seeds under one name, or a name both seeded and retired (in either order), raises
    loudly. Reset each ``start()`` so a reload re-registers cleanly."""

    def __init__(self) -> None:
        self._seeds: dict[str, StateModuleDocument] = {}
        self._retired: set[str] = set()

    def register(self, doc: StateModuleDocument) -> None:
        if doc.name in self._seeds:
            raise ValueError(f"state-module seed {doc.name!r} is already registered")
        if doc.name in self._retired:
            raise ValueError(f"state-module {doc.name!r} is registered as retired — it cannot also be seeded")
        self._seeds[doc.name] = doc

    def register_retired(self, name: str) -> None:
        if name in self._seeds:
            raise ValueError(f"state-module {name!r} is registered as a seed — it cannot also be retired")
        self._retired.add(name)

    def seeds(self) -> list[StateModuleDocument]:
        return list(self._seeds.values())

    def retired(self) -> set[str]:
        return set(self._retired)

    def reset(self) -> None:
        self._seeds.clear()
        self._retired.clear()


def _canonical_hash(body: dict) -> str:
    """A byte-stable content hash of a module body — the shipped-vs-edited discriminator."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def apply_module_seeds(
    store: PostgresStatesStore,
    *,
    seeds: Iterable[StateModuleDocument],
    retired: Iterable[str],
) -> None:
    """Reconcile the module store against the given seeds and retired names. Idempotent — a
    second run with an unchanged body is a no-op."""
    for doc in seeds:
        body = doc.model_dump(by_alias=True)
        shipped_hash = _canonical_hash(body)
        stored = await store.get_module(doc.name)
        if stored is None:
            await store.upsert_module(doc.name, body, shipped_hash)
            logger.info("state-module seed %r: created", doc.name)
            continue
        stored_hash = stored.get("shipped_hash")
        if stored_hash is not None and _canonical_hash(stored["body"]) == stored_hash:
            if shipped_hash != stored_hash:
                await store.upsert_module(doc.name, body, shipped_hash)
                logger.info("state-module seed %r: upgraded (shipped body drifted)", doc.name)
        else:
            logger.info("state-module seed %r: operator-edited — left untouched", doc.name)

    for name in retired:
        stored = await store.get_module(name)
        if stored is None:
            continue
        stored_hash = stored.get("shipped_hash")
        if stored_hash is None or _canonical_hash(stored["body"]) != stored_hash:
            logger.info("retired state-module seed %r: operator-edited — kept", name)
            continue
        if await store.list_mounts_of_module(name):
            logger.warning("retired state-module seed %r is still mounted — kept until unmounted", name)
            continue
        await store.delete_module(name)
        logger.info("retired state-module seed %r: deleted", name)


__all__ = ["StateModuleSeedRegistry", "apply_module_seeds"]
