"""The served-document registry, the bundle builder, and the bundle's on-disk form.

:data:`SERVED_DOCUMENTS` is EXPLICIT — an ordered published-name → model mapping —
rather than "every model in the contract", so an internal or private model can never
leak into the public artifact. :func:`build_document_schemas` turns it into the
versioned bundle; :func:`bundle_json` is the canonical serialization the exporter
writes; :func:`bundle_drift` judges freshness on the parsed structure — the documents
and the shared ``$defs`` — with the ``contract_version`` envelope field checked
separately, so a version-only bump never reports as a shape change.
"""

from __future__ import annotations

import json
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tai42_contract.access_control.models import AccessPolicy, RoleDefinition
from tai42_contract.agent.base import SubAgentSpec
from tai42_contract.backend.callback import CallbackSchema
from tai42_contract.channels import ChannelTemplate
from tai42_contract.conversations import (
    ConversationRoute,
    ConversationRouteCreate,
    TargetConversationConfig,
)
from tai42_contract.hooks.models import HookParams, HookRegister, HookSubject
from tai42_contract.presets.models import PresetBody, PresetSeed
from tai42_contract.states.binding import StateAttach, StateBinding, StateInjection, StateUpdate
from tai42_contract.states.models import StateDeclaration, StateTemplateDocument

#: The published served documents: an explicit, ordered ``published name -> contract
#: model`` mapping for exactly the documents the operator surface serves and an editor
#: reads and writes. Each key is the stable published name; a document's wire shape is
#: the model's ``model_json_schema()``, markers included.
SERVED_DOCUMENTS: dict[str, type[BaseModel]] = {
    "PresetBody": PresetBody,
    "PresetSeed": PresetSeed,
    "HookSubject": HookSubject,
    "HookRegister": HookRegister,
    "HookParams": HookParams,
    "AccessPolicy": AccessPolicy,
    "RoleDefinition": RoleDefinition,
    "ConversationRouteCreate": ConversationRouteCreate,
    "ConversationRoute": ConversationRoute,
    "TargetConversationConfig": TargetConversationConfig,
    "ChannelTemplate": ChannelTemplate,
    "StateDeclaration": StateDeclaration,
    "StateTemplateDocument": StateTemplateDocument,
    "StateInjection": StateInjection,
    "StateUpdate": StateUpdate,
    "StateAttach": StateAttach,
    "StateBinding": StateBinding,
    "SubAgentSpec": SubAgentSpec,
    "CallbackSchema": CallbackSchema,
}

#: The bundle's committed location relative to the package root, and the JSON-schema
#: ``$ref`` target every shared definition resolves against.
BUNDLE_RESOURCE = ("schemas", "contract-schema.json")
_REF_TEMPLATE = "#/$defs/{model}"


def build_document_schemas() -> dict[str, Any]:
    """The served-document JSON-schema bundle.

    For each entry in :data:`SERVED_DOCUMENTS` the model's ``model_json_schema()`` is
    dumped with a stable ``$ref`` template; every model's shared definitions (chiefly
    :class:`~tai42_contract.template.TemplatedText`) are hoisted into ONE top-level
    ``$defs`` block so a ``$ref`` resolves once. Two documents that define the SAME
    ``$def`` name with DIFFERENT schemas raise loudly — a silent overwrite would publish
    one document's shape under another's reference.

    The envelope carries the exact ``contract_version`` it was cut from, so a consumer
    pins the shape to a released contract.
    """
    defs: dict[str, Any] = {}
    documents: dict[str, Any] = {}
    for name, model in SERVED_DOCUMENTS.items():
        schema = model.model_json_schema(ref_template=_REF_TEMPLATE)
        for def_name, def_schema in schema.pop("$defs", {}).items():
            existing = defs.get(def_name)
            if existing is not None and existing != def_schema:
                raise ValueError(
                    f"shared $def {def_name!r} maps to two distinct schemas across served documents — "
                    "a definition-name collision"
                )
            defs[def_name] = def_schema
        documents[name] = schema
    return {
        "contract_version": version("tai42-contract"),
        "$defs": defs,
        "documents": documents,
    }


def bundle_json(bundle: dict[str, Any]) -> str:
    """The canonical on-disk form of a bundle — sorted keys, two-space indent, non-ASCII
    left un-escaped, a trailing newline. Emitting raw UTF-8 rather than ``\\uXXXX``
    escapes keeps the committed bytes identical to what the release tooling's JSON
    version-bump rewrites, so cutting a release touches only the ``contract_version`` line.
    """
    return json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def bundle_drift(fresh: dict[str, Any], committed: dict[str, Any]) -> list[str]:
    """The loud, distinct reasons the committed bundle is stale against a fresh rebuild,
    or an empty list when it is fresh.

    Freshness is judged on the parsed STRUCTURE, never on bytes: each served document and
    the shared ``$defs`` block of the committed bundle must equal the fresh build's. The
    ``contract_version`` envelope is a SEPARATE equality — the committed field must match
    the version the fresh build was cut from (the running package version) — so a
    version-only bump (which the release tooling applies to ``contract_version``) is
    reported as a version mismatch, distinct from any shape drift, and formatting never
    enters the judgment.
    """
    lines: list[str] = []
    fresh_version = fresh.get("contract_version")
    committed_version = committed.get("contract_version")
    if committed_version != fresh_version:
        lines.append(
            f"contract_version {committed_version!r} in the committed bundle does not match "
            f"the running package version {fresh_version!r}"
        )
    fresh_docs = fresh.get("documents", {})
    committed_docs = committed.get("documents", {})
    for name in sorted(set(fresh_docs) | set(committed_docs)):
        if name not in committed_docs:
            lines.append(f"document {name!r} is new")
        elif name not in fresh_docs:
            lines.append(f"document {name!r} was removed")
        elif fresh_docs[name] != committed_docs[name]:
            lines.append(f"document {name!r} changed shape")
    if fresh.get("$defs") != committed.get("$defs"):
        lines.append("the shared $defs block changed")
    return lines


def committed_bundle_path() -> Path:
    """The committed bundle inside the installed package (the repo source under an
    editable install, the wheel's package data otherwise)."""
    resource = files("tai42_contract")
    for part in BUNDLE_RESOURCE:
        resource = resource.joinpath(part)
    return Path(str(resource))
