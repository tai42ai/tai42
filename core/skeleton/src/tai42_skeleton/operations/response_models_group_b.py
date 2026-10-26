"""Response models for the group-B skeleton operations.

Covers config, hooks, connectors, templates, tools, tool_meta, tool_extensions, tool_runs, roles, storage,
schedules.

Each model DESCRIBES the inner payload a route returns — the shape the adapter
wraps in the ``{"data": ...}`` success envelope — and never re-declares the envelope or
reshapes a wire body. Shared cross-package models (``ApplyResponse``, ``FanoutSummary``,
``ProfileApplyResponse``, ``OpaqueJson``) live in ``tai42_contract.app.responses`` and are
imported by the operations directly; this module holds the models local to group B.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator
from tai42_contract.app.responses import FanoutSummary
from tai42_contract.backend import ScheduleListRow
from tai42_contract.connectors.models import StartConnectNoAuthResponse, StartConnectResponse
from tai42_contract.hooks import HookParams
from tai42_contract.manifest import ExtensionElement
from tai42_contract.settings_profiles import SettingsProfileBody
from tai42_contract.tool_meta import FolderRecord, ToolMetaRecord
from tai42_contract.versioning import DocumentVersion
from tai42_kit.settings import SettingsFieldInfo

from tai42_skeleton.hooks.trigger_auth import TriggerAuth
from tai42_skeleton.operations.response_models_group_c import ExtensionView

# -- config ------------------------------------------------------------------


class EnvView(BaseModel):
    """The stored env map plus the operator's masked-key marks and the stored keys the deployment sets.

    ``env`` values are verbatim (masking is display-side, never on the wire); ``secret_keys`` names which
    keys the UI masks; ``deployment_keys`` names the stored keys the deployment sets, which a write may
    remove or carry unchanged but never set to a new value.
    """

    env: dict[str, str]
    secret_keys: list[str]
    deployment_keys: list[str]


class ConfigModeView(BaseModel):
    """The active config backend mode: ``file`` (built in) or an external provider's mode name."""

    config_mode: str


class SettingsFieldView(SettingsFieldInfo):
    """One settings field: its registered metadata, the value it resolves to, and the layer that supplied it.

    ``value_source``: ``"deployment"`` (the deployment env sets the field; it is locked, and a save that
    sets it is refused), ``"stored"`` (the stored env), ``"default_namespace"`` (a ``TAI_DEFAULT_*``
    fallback), ``"field_default"`` (the bare field default), or ``null`` for a nested-group reference,
    which carries no value of its own.
    """

    value: JsonValue
    value_source: Literal["deployment", "stored", "default_namespace", "field_default"] | None


class SettingsGroupView(BaseModel):
    """One registered settings group with each field's resolved value."""

    name: str
    module: str
    qualname: str
    fields: list[SettingsFieldView]


class SettingsSchemaView(BaseModel):
    """Every registered settings group with its per-field resolved values."""

    groups: list[SettingsGroupView]


class ProfileSummary(BaseModel):
    """One settings-profile listing row."""

    name: str
    description: str


class ProfileListResponse(RootModel[list[ProfileSummary]]):
    """The bare array of settings-profile listing rows."""


class ProfileWriteResult(BaseModel):
    """A profile write/rollback confirmation — the new active version, never the body.

    So a secret never re-emits on the write path.
    """

    ok: bool
    version: int


class OkResult(BaseModel):
    """A bare ``{ok}`` confirmation."""

    ok: bool


class ProfileDiffChange(BaseModel):
    """One changed env key in a profile diff.

    ``old``/``new`` are the env VALUES on each side (real values — the UI masks); NEVER widened where rendered.
    """

    key: str
    old: str
    new: str


class ProfileDiff(BaseModel):
    """A settings-profile-vs-stored-env diff.

    ``added``/``removed``/``recycle_keys``/``refused_keys`` are key names; ``changed`` carries the per-key value change.
    """

    added: list[str]
    removed: list[str]
    changed: list[ProfileDiffChange]
    recycle_keys: list[str]
    refused_keys: list[str]


class ProfileVersionSummary(BaseModel):
    """One row of a settings-profile version history.

    ``created_at`` is an ISO-8601 timestamp string; ``is_current`` marks the active version.
    """

    version: int
    tags: list[str]
    created_at: str
    is_current: bool


class ProfileVersionListResponse(RootModel[list[ProfileVersionSummary]]):
    """The bare array of settings-profile version-history rows."""


class ProfileVersionView(BaseModel):
    """One settings-profile version row extended with its full ``body``.

    Real env values — this door is secret-fenced; masking is display-side only.
    """

    version: int
    tags: list[str]
    created_at: str
    is_current: bool
    body: SettingsProfileBody


# -- hooks -------------------------------------------------------------------


class HookListView(BaseModel):
    """The registered hooks plus the live per-topic verifier bindings and derived trigger-auth axis.

    ``topic_verifiers`` values are dumped binding metadata (an open,
    secret-adjacent map, kept opaque so a bound config is not widened onto the wire);
    ``trigger_auth`` maps each visible topic to how its webhook ingress door
    authenticates, derived live, never stored.
    """

    items: list[HookParams]
    total: int
    topic_verifiers: dict[str, JsonValue]
    trigger_auth: dict[str, TriggerAuth]


class HookRegisterResult(BaseModel):
    """A hook upsert confirmation — ``registered`` is ``True`` for a create and a replace alike."""

    registered: bool
    name: str


class RemovedByName(BaseModel):
    """A remove-by-name confirmation (hook unregister, trigger-link delete)."""

    removed: bool
    name: str


class TopicVerifierView(BaseModel):
    """A topic-to-verifier binding confirmation."""

    topic: str
    verifier: str


class RemovedByTopic(BaseModel):
    """A remove-by-topic confirmation (topic-verifier unbind)."""

    removed: bool
    topic: str


class TriggerLinkCreated(BaseModel):
    """A freshly minted trigger link.

    ``token`` is a ONE-TIME secret — it appears only here (nothing else stores or lists it); typing must not
    widen where it is logged. ``expires_at`` is an ISO-8601 timestamp string, or ``null`` for a permanent link.
    """

    name: str
    trigger_path: str
    token: str
    topic: str
    expires_at: str | None


class TriggerLinkView(BaseModel):
    """One listed trigger link: the stored record plus its token-hash PREFIX and derived ``trigger_auth`` axis.

    Never a raw token — none is stored. ``created_at`` is an ISO-8601 timestamp string; ``expires_at`` is one
    or ``null`` for a permanent link.
    """

    name: str
    topic: str
    execution_key: str
    execution_key_fingerprint: str
    require_api_key: bool
    tool_kwargs: dict[str, JsonValue] | None
    created_by: str | None
    created_at: str
    expires_at: str | None
    token_hash_prefix: str
    trigger_auth: TriggerAuth


class TriggerLinkListView(BaseModel):
    """Every live trigger link."""

    items: list[TriggerLinkView]
    total: int


class HookRunClaimIntentView(BaseModel):
    """A resume or take claim a hook run's hook announced and did not confirm."""

    action: Literal["resume", "take"]
    ids: list[str]


class HookRunView(BaseModel):
    """One kept hook run, without its event payload or its start spec (either may carry secrets of the sender).

    ``status`` is ``pending`` (due at ``next_attempt_at``), ``running`` (an attempt holds its lease) or
    ``failed`` (held until an operator retries or discards it). ``acted`` says whether and how the
    hook acted on its parked entries: ``""`` not at all, ``start`` started its tool, ``cancel``,
    ``resume`` or ``take`` on ``acted_ids``. ``claim_intent`` is set while a claim is still to be
    settled, and on a failed run whose claim could not be told.
    """

    id: str
    topic: str
    hook: str
    status: Literal["pending", "running", "failed"]
    attempts: int
    next_attempt_at: datetime
    acted: Literal["", "start", "cancel", "resume", "take"]
    acted_ids: list[str]
    claim_intent: HookRunClaimIntentView | None
    last_error: str | None
    last_error_kind: str | None
    created_at: datetime
    failed_at: datetime | None


class HookRunsPage(BaseModel):
    """One page of hook runs with the totals of every outstanding and every failed run.

    Outstanding runs come first, the soonest due first; failed runs follow, the most recently
    failed first. ``next_cursor`` is the cursor the next page reads from, ``null`` on the last page.
    """

    items: list[HookRunView]
    next_cursor: str | None
    outstanding: int
    failed: int


class HookRunDiscarded(BaseModel):
    """A discarded failed hook run's id."""

    discarded: str


class StringListResponse(RootModel[list[str]]):
    """A bare array of strings (verifier names, template ids, tool names)."""


# -- connectors --------------------------------------------------------------


class StartConnectOutcome(RootModel[StartConnectResponse | StartConnectNoAuthResponse]):
    """A Connect start.

    Either an OAuth authorize URL (``StartConnectResponse``) or an immediate no-auth connection
    (``StartConnectNoAuthResponse``).
    """


class ConnectorReencryptResult(BaseModel):
    """The KEK re-encrypt sweep's outcome, for the connection tokens and the stored client credentials.

    ``scanned`` connection blobs split into ``reencrypted`` (rewritten under the current KEK), ``skipped``
    (already under the current key), and ``failed`` (no configured key could open them, or compare-and-set
    contention was not resolved); ``failed_connection_ids`` names each failed connection. The ``clients_*``
    counts split the stored OAuth client blobs the same way, ``failed_client_row_ids`` naming each failed
    client row. ``cas_retries`` counts the compare-and-set retries concurrent writers forced in both tables.
    """

    scanned: int
    reencrypted: int
    skipped: int
    failed: int
    failed_connection_ids: list[str]
    clients_scanned: int
    clients_reencrypted: int
    clients_skipped: int
    clients_failed: int
    failed_client_row_ids: list[str]
    cas_retries: int


# -- templates ---------------------------------------------------------------


class StoredTextEntry(BaseModel):
    """A stored object the classifier reads as text, with its UTF-8 content."""

    kind: Literal["text"]
    id: str
    content_type: str | None
    size: int
    content: str


class StoredDocumentEntry(BaseModel):
    """A stored document (PDF, DOCX, …): ``content`` is the text extracted from it, not its stored bytes."""

    kind: Literal["document"]
    id: str
    content_type: str | None
    size: int
    content: str


class StoredBinaryEntry(BaseModel):
    """A stored binary object: its metadata, with no content."""

    kind: Literal["binary"]
    id: str
    content_type: str | None
    size: int


StoredEntryView = Annotated[StoredTextEntry | StoredDocumentEntry | StoredBinaryEntry, Field(discriminator="kind")]


class StoredEntry(RootModel[StoredEntryView]):
    """One stored object as a text, document or binary entry."""


class TemplateTextEntry(StoredTextEntry):
    """A text entry with its inferred input schema, or the template error that stopped the inference.

    The ``schema`` object is an inferred JSON schema (it may carry an ``x-tai42-inference: partial``
    marker when type inference is incomplete). Exactly one of ``schema`` and ``schema_error`` is set.
    The Python attribute is suffixed to avoid shadowing a ``BaseModel`` member; the wire key stays
    ``schema`` via the alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_: dict[str, JsonValue] | None = Field(alias="schema")
    schema_error: str | None

    @model_validator(mode="after")
    def _one_of_schema_or_error(self) -> TemplateTextEntry:
        if (self.schema_ is None) == (self.schema_error is None):
            raise ValueError("a text template entry carries exactly one of 'schema' and 'schema_error'")
        return self


class TemplateDocumentEntry(StoredDocumentEntry):
    """A document entry; its extracted text is not a template, so it has no schema."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: None = Field(default=None, alias="schema")


class TemplateBinaryEntry(StoredBinaryEntry):
    """A binary entry; it is not a template, so it has no schema."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: None = Field(default=None, alias="schema")


TemplateEntryView = Annotated[
    TemplateTextEntry | TemplateDocumentEntry | TemplateBinaryEntry, Field(discriminator="kind")
]


class TemplateEntry(RootModel[TemplateEntryView]):
    """One stored object as a template entry: the stored entry plus its inferred input schema."""


class TemplateUploadResult(BaseModel):
    """A template upload confirmation with the fleet cache-eviction summary."""

    path: str
    uploaded: bool
    fanout: FanoutSummary


class TemplateDeleteResult(BaseModel):
    """A template (or template-directory) delete confirmation with the fleet cache-eviction summary."""

    path: str
    deleted: bool
    fanout: FanoutSummary


class RenderedTemplate(BaseModel):
    """A rendered template."""

    rendered: str


class CacheClearResult(BaseModel):
    """A template-cache clear confirmation with the fleet cache-eviction summary."""

    cleared: bool
    fanout: FanoutSummary


# -- tools -------------------------------------------------------------------


class ToolTagRow(BaseModel):
    """One tool's declared tags, visibility, and capability badges.

    ``hidden`` and ``badges`` are the tool's OWN declaration (before the tool_meta overlay's override).
    ``schedulable`` is whether the tool's schedule vehicle is registered (the scheduling backend's
    ``schedule_task`` extension is attached to it).
    """

    name: str
    tags: list[str]
    hidden: bool
    badges: list[str]
    schedulable: bool


class ToolTagListResponse(RootModel[list[ToolTagRow]]):
    """The bare array of per-tool tag rows."""


class ScheduleListResponse(RootModel[list[ScheduleListRow]]):
    """The bare array of the scheduling backend's typed schedule rows."""


class ToolSchemaView(BaseModel):
    """One tool's input/output JSON schemas and description.

    ``input``/``output`` are arbitrary JSON-schema objects; ``output`` and ``description`` are null when the tool
    declares none.
    """

    input: dict[str, JsonValue]
    output: dict[str, JsonValue] | None
    description: str | None


class ToolsSchemaMap(RootModel[dict[str, ToolSchemaView]]):
    """Every tool's schema, keyed by tool name."""


# -- tool_meta ---------------------------------------------------------------


class ToolMetaListView(BaseModel):
    """The whole tool-metadata overlay in one read: the flat folder tree plus every per-tool row."""

    folders: list[FolderRecord]
    meta: list[ToolMetaRecord]


class ToolMetaDeleted(BaseModel):
    """A tool-overlay-row delete confirmation."""

    tool_name: str
    deleted: bool


class FolderDeleted(BaseModel):
    """A folder delete confirmation."""

    folder_id: str
    deleted: bool


# -- tool_extensions ---------------------------------------------------------


class ToolExtensionsView(BaseModel):
    """A tool's applied extension combos plus the catalog of available extensions.

    Each ``available`` entry is one registered extension's catalog row.
    """

    combos: list[list[ExtensionElement]]
    available: list[ExtensionView]


class ToolExtensionCombosModified(ToolExtensionsView):
    """A combo edit's reply: the tool's new combos and the extension catalog, plus the write's span release.

    ``span_release_error`` is ``null`` when the config store released its exclusive write span after the persist;
    otherwise the store's error — the change landed and its reload and broadcast still ran, but the store may still
    hold its write span until the writer process exits, so a later write can block or be refused until then.
    """

    span_release_error: str | None


# -- tool_runs ---------------------------------------------------------------


class RunSubmitted(BaseModel):
    """A background tool-run submission acknowledgement (served with ``202``)."""

    run_id: str


class ToolRunView(BaseModel):
    """A background tool run's full status view.

    ``finished_at``/``result``/``error``/``resumed_interactions`` are present only once the run reaches a
    terminal status, so they are optional; ``result`` is the tool's arbitrary output;
    ``resumed_interactions`` is the parked interaction ids the run resumed or took while it executed
    (``[]`` when it resumed nothing).

    ``outcome`` is present only on a ``failed`` record whose run reached a failed terminal (a run that
    raises the contract's ``RunTerminalFailed``): the driver's outcome, whole, as the sync run-tool door
    delivers it under the same name; ``error`` is then the plain ``tool run failed``. A plain raise records
    its message under ``error`` and no ``outcome``.
    """

    run_id: str
    tool_name: str
    status: str
    started_at: str
    finished_at: str | None = None
    result: JsonValue | None = None
    error: str | None = None
    outcome: JsonValue | None = None
    resumed_interactions: list[str] | None = None


class ToolRunListItem(BaseModel):
    """One background tool run in the list view — never ``result``/``error``.

    ``finished_at`` is present only for a terminal run.
    """

    run_id: str
    tool_name: str
    status: str
    started_at: str
    finished_at: str | None = None


class ToolRunListResponse(RootModel[list[ToolRunListItem]]):
    """The bare array of a tool's recent runs, newest first."""


# -- roles -------------------------------------------------------------------


class RoleDeleted(BaseModel):
    """A role delete confirmation."""

    name: str
    deleted: bool


class RoleVersionsView(BaseModel):
    """A role's append-only version history plus its who/when/before-after audit trail.

    ``audit`` rows are open audit-event metadata (secret-fenced, kept opaque).
    """

    versions: list[DocumentVersion]
    audit: list[dict[str, JsonValue]]


# -- storage -----------------------------------------------------------------


class StorageInfo(BaseModel):
    """The storage provider identity, or ``present: false`` when none is installed.

    ``surfaces`` names the management routers this deployment mounts over the store.
    """

    present: bool
    provider: str | None
    module: str | None
    surfaces: list[Literal["storage", "templates"]]


class ResourceList(BaseModel):
    """The sorted storage resource ids under a keyed envelope (the wire shape as-is)."""

    resources: list[str]


class ResourceStat(BaseModel):
    """One storage resource's inferred content type; ``null`` when the provider cannot infer one."""

    id: str
    content_type: str | None


class ResourceStored(BaseModel):
    """A storage-resource upload confirmation."""

    id: str
    stored: bool


class ResourceDeleted(BaseModel):
    """A storage-resource delete confirmation."""

    id: str
    deleted: bool


class DirDeleted(BaseModel):
    """A storage-directory delete confirmation."""

    dir: str
    deleted: bool
