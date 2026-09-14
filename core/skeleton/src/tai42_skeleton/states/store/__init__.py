"""The thin Postgres seam over the kit DB registry (component ``states``).

ALL SQL lives here; service and route code never write SQL. The subject-keyed record
substrate: ``state_declarations`` (base ``schema`` + composed ``effective_schema`` +
the ``subject_kinds`` and ``default_subject_kind`` it serves), ``state_records``,
``state_applied_ops`` (the idempotency ledger), ``state_subject_aliases``,
``state_templates``, ``state_attachments``, and ``state_writes`` (the write provenance
ledger). Connection settings resolve FRESH per operation (``component_store_settings``)
so a config reload re-targets the store.

A SUBJECT is ``(target_kind, target_name, kind, key)`` — four columns everywhere;
equality (and identity across every method) is all four under ``state``. The store
takes a full :class:`~tai42_contract.states.StateSubject` and refuses anything less;
subject resolution belongs to the doors, never here.

SUBJECT ALIASES: a fold leaves ONE live record and an alias row, and every record
access resolves the subject through the alias table INSIDE its own transaction, so an
old key keeps landing on the surviving record. Resolution is ONE hop by invariant:
``fold`` flattens every alias that pointed at the folded subject onto the new
canonical. Folds serialize against writes on the declaration row (``FOR UPDATE`` vs
``apply_ops``'s ``FOR SHARE``). Aliases are IDENTITY, not data: the retention sweep
never touches them; an erase deletes the surviving record AND every alias pointing at
it; a declaration delete cascades them.

Writes stamp ``updated_at = clock_timestamp()`` (statement time, taken while the row
lock is HELD, so it is commit-ordered per record) and return it as ``seq`` — the
channel ordering key. Every record-changing door records one ``state_writes`` row in
the same transaction with the write's COMPLETED origin (``door``/``actor`` stamped by
the platform chokepoint) and the absolute paths it touched — the audit of who wrote
what is never optional.
"""

from __future__ import annotations

# The pooled-client seam and the settings reader are re-exported here — bound before the
# concern submodules import — so a test's package-alias monkeypatch (``client_ctx`` for the
# fake transport, ``states_settings`` for the retention window) is read through this package
# at call time and bites.
from tai42_kit.clients import client_ctx

from tai42_skeleton.states.db import states_settings

from .cursors import make_cursor
from .retention import store_settings_default_retention, store_settings_retention
from .store import PostgresStatesStore

# ``_iso_now`` / ``_traced_paths`` keep their original ``states.store`` public path (the pure
# trace helpers are imported from here by the service test suite); the redundant alias marks
# the re-export explicit.
from .trace import _iso_now as _iso_now
from .trace import _traced_paths as _traced_paths
from .trace import stamp_trace

__all__ = [
    "PostgresStatesStore",
    "client_ctx",
    "make_cursor",
    "stamp_trace",
    "states_settings",
    "store_settings_default_retention",
    "store_settings_retention",
]
