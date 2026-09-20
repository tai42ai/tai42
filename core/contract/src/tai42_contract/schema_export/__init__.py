"""The published served-document JSON-schema bundle.

The operator surface serves a fixed set of platform DOCUMENTS an editor reads and
writes — presets, hooks, access policies, conversation routes, channel templates,
state declarations and templates, sub-agent specs, callback schemas, and the state
bindings. :data:`SERVED_DOCUMENTS` is the explicit, ordered registry of published
name → contract model for exactly those documents; :func:`build_document_schemas`
dumps each model's JSON schema into ONE versioned bundle with a shared ``$defs``
block, so an editor generates its validators from the platform's own shapes rather
than re-describing them by hand.

The bundle is a pure function of the contract models — no app, no routes, no
database or Redis — so it builds in the contract's own test job, and a committed
copy travels in the wheel (``schemas/contract-schema.json``). The freshness gate
rebuilds it and diffs the committed copy, so a served-document shape change that
forgets to regenerate reds the build.
"""

from __future__ import annotations

from tai42_contract.schema_export.registry import (
    SERVED_DOCUMENTS,
    build_document_schemas,
    bundle_json,
    committed_bundle_path,
)

__all__ = [
    "SERVED_DOCUMENTS",
    "build_document_schemas",
    "bundle_json",
    "committed_bundle_path",
]
