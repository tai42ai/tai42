"""Response models for the Group C ``@operation`` success bodies.

Each model DESCRIBES the inner payload a Group C operation returns today (the
adapter wraps it in the ``{"data": ...}`` envelope — the models never re-declare
that envelope and never reshape a wire body). Genuinely-open sub-fields (an
agent's input JSON schema, a trace's free-form input/output/metadata, a
verifier-stripped ``format_payload``) are typed ``JsonValue``; a bare list body is
a named ``RootModel[list[...]]`` subclass and a dynamic slug-keyed map a named
``RootModel[dict[...]]`` subclass, so every body carries a stable, uniquely-named
component schema. Existing models (``FanoutSummary``, ``KindStatus``,
``LoginMethod``, ``RouteConfig``, ``StudioPluginManifest`` and the channel/media
contract shapes) are reused, never re-authored.
"""

from __future__ import annotations

import warnings

from pydantic import BaseModel, JsonValue, RootModel
from tai42_contract.accounts.models import LoginMethod
from tai42_contract.app.responses import FanoutSummary
from tai42_contract.channels import ChannelTemplate, Option, OptionSection
from tai42_contract.interactions.models import LocationElement, MediaItem
from tai42_contract.sub_mcp import RouteConfig

from tai42_skeleton.app.kind_status import KindStatus
from tai42_skeleton.plugins.registry import StudioPluginManifest

# --- Agents -----------------------------------------------------------------


class AgentView(BaseModel):
    """One registered agent. ``input_schema`` is the agent's run-tool input JSON
    schema (``ToolInput.model_json_schema()``), a genuinely-open schema object;
    ``spec_runnable`` is the read authorable-marker, never inferred from a name."""

    name: str
    description: str
    tool_name: str
    input_schema: JsonValue
    spec_runnable: bool


class AgentListing(BaseModel):
    """The agent-catalog body (``list_agents`` and the spec-runnable filter share
    it): every listed agent plus their ``total`` count."""

    items: list[AgentView]
    total: int


# --- Backend ----------------------------------------------------------------


class BackendInfo(BaseModel):
    """Backend identity. ``backend``/``module`` are ``null`` when no provider is
    registered (``present`` false)."""

    present: bool
    backend: str | None = None
    module: str | None = None


# --- Backup -----------------------------------------------------------------


class BackupSectionInfo(BaseModel):
    """One registered backup section: its ``name`` and whether it holds secrets."""

    name: str
    secret: bool


class BackupSectionListing(RootModel[list[BackupSectionInfo]]):
    """The bare-list body of ``list_sections`` — one entry per registered section."""


class BackupSectionReport(BaseModel):
    """One section's per-import counts plus any per-record ``errors``. ``fanout`` is
    present ONLY for the templates section (a template restore fans a cache-evict
    across the fleet); every other section omits it."""

    created: int
    updated: int
    skipped: int
    skipped_existing: int
    errors: list[str]
    fanout: FanoutSummary | None = None


class BackupImportResult(BaseModel):
    """The import report: ``ok`` false when any selected section errored, and the
    per-section reports keyed by section name."""

    ok: bool
    sections: dict[str, BackupSectionReport]


# --- Channels ---------------------------------------------------------------


class ChannelListing(BaseModel):
    """The registered channel names the delivery media can resolve."""

    channels: list[str]


# --- Checkpoints ------------------------------------------------------------


class CheckpointSweepResult(BaseModel):
    """The checkpoint-sweep report. ``skipped`` is present ONLY on a no-op branch
    (an unsweepable provider or an unset TTL); a real sweep omits it and reports
    the swept threads."""

    provider: str
    ttl_minutes: int | None = None
    swept_count: int
    swept_threads: list[str]
    skipped: str | None = None


# --- Extensions -------------------------------------------------------------


class ExtensionView(BaseModel):
    """One registered extension: its ``name`` and lowercase ``kind`` enum value."""

    name: str
    kind: str


class ExtensionListing(RootModel[list[ExtensionView]]):
    """The bare-list body of ``list_extensions`` — one entry per extension."""


# --- Interactions -----------------------------------------------------------


class InteractionActionResult(BaseModel):
    """A single interaction terminal result: the id and its new ``status``
    (``answered`` for the answer door, ``cancelled`` for the cancel door)."""

    interaction_id: str
    status: str


class InteractionFrame(BaseModel):
    """One pending question's client add-frame (the paged list door and the live
    tail share it). ``format_payload`` is the verifier-stripped, otherwise-open
    payload (``null`` when the question carries none). ``server_verified`` rides
    ONLY when a verifier was stripped; ``channel``/``recipient``/``origin``/
    ``audience``/``media`` ride only when the question set them (absent otherwise)."""

    interaction_id: str
    group_id: str
    question: str
    answer_format: str
    format_payload: JsonValue
    created_at: str
    timeout_at: str
    sensitive: bool
    server_verified: bool | None = None
    channel: str | None = None
    recipient: str | None = None
    origin: str | None = None
    audience: str | None = None
    media: list[MediaItem] | None = None


class InteractionWindow(BaseModel):
    """One page of pending questions. ``truncated`` is always false (the pending
    index is the whole set, sliced in memory); ``next_page`` is ``null`` on the
    last page."""

    items: list[InteractionFrame]
    total: int
    page: int
    page_size: int
    next_page: int | None = None
    truncated: bool


class PendingInteraction(BaseModel):
    """One parked (async) ask on the audit surface. ``channel``/``recipient``/
    ``audience``/``thread_id``/``expiry_at`` are nullable (a park may carry none);
    ``question`` is truncated to a preview."""

    interaction_id: str
    group_id: str
    question: str
    channel: str | None = None
    recipient: str | None = None
    audience: str | None = None
    thread_id: str | None = None
    expiry_at: str | None = None
    created_at: str
    mode: str


class PendingInteractionListing(BaseModel):
    """The parked-interactions audit body: the bounded (and, for a restricted
    caller, audience-filtered) slice plus its ``count``."""

    items: list[PendingInteraction]
    count: int


# --- Login ------------------------------------------------------------------


class LoginMethodsListing(BaseModel):
    """The aggregated login surface. Each method is dumped ``exclude_none`` so an
    unset optional (icon/autocomplete) is OMITTED, never ``null``; ``bootstrap`` is
    true while a create-owner screen is still needed."""

    methods: list[LoginMethod]
    bootstrap: bool


class ClaimExchangeResult(BaseModel):
    """The one-time claim-token exchange result. ``token`` is a raw, one-time API
    key — a SECRET; it must never be logged or rendered where a key leaks."""

    token: str
    user_id: str


class LogoutResult(BaseModel):
    """The logout confirmation — success-only (a non-revocable session is a 404)."""

    revoked: bool


# --- Notifications ----------------------------------------------------------


with warnings.catch_warnings():
    # ``schema`` intentionally shadows pydantic's deprecated ``BaseModel.schema``
    # alias: it is the stored record's wire key (an ask-less form's answer schema),
    # so the field name must match. Suppressed narrowly at the definition site, the
    # same pattern the ``NotifyUser`` request model uses for its ``schema`` field.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class NotificationRecord(BaseModel):
        """One stored internal-sink notification. ``recipient``/``audience`` and the
        richer-send forms (``media``/``template``/``options``/``location``/
        ``sections``/``header``/``footer``/``schema``) are ``null`` on a plain
        record; ``schema`` (an open answer-schema object) rides only from the
        channel path's feed write. ``id``/``created_at`` are server-minted."""

        id: str
        message: str
        recipient: str | None = None
        audience: str | None = None
        media: list[MediaItem] | None = None
        template: ChannelTemplate | None = None
        options: list[Option] | None = None
        location: LocationElement | None = None
        sections: list[OptionSection] | None = None
        header: MediaItem | None = None
        footer: str | None = None
        schema: JsonValue = None  # pyright: ignore[reportIncompatibleMethodOverride]
        created_at: str


class NotificationListing(BaseModel):
    """The internal notifications feed, newest-first (empty on an unconfigured
    store)."""

    notifications: list[NotificationRecord]


class NotifyResult(RootModel[str]):
    """The notify-user body: a bare confirmation STRING (the adapter envelopes it
    as ``{"data": <str>}``). Typed as a string rather than wrapped, so the wire
    body is unchanged."""


# --- Observability ----------------------------------------------------------


class MetricsSummary(BaseModel):
    """The dashboard summary tile derived from the ungrouped metrics row.
    ``timeToFirstTokenMs`` is always ``null`` (no neutral measure for it — kept for
    a stable shape)."""

    totalRuns: int
    totalCost: float
    totalTokens: int
    averageLatencyMs: int
    avgCostPerRun: float
    avgTokensPerRun: int
    timeToFirstTokenMs: int | None = None


class MetricsTimePoint(BaseModel):
    """One granularity bucket of the dashboard series. ``bucket`` is the backend's
    time-field value, ``null`` when the row exposes no ISO-like bucket."""

    bucket: str | None = None
    runs: int
    cost: float
    avgLatencyMs: int
    totalTokens: int


class ModelUsageRow(BaseModel):
    """One per-model usage row (top 8 by cost); ``model`` is ``unknown`` when the
    backend named none."""

    model: str
    calls: int
    cost: float
    totalTokens: int
    avgLatencyMs: int


class MetricsResult(BaseModel):
    """The metrics body: the summary tile, the granularity series, the (optional,
    possibly-empty) per-model breakdown, and the resolved ``granularity``."""

    summary: MetricsSummary
    timeSeries: list[MetricsTimePoint]
    byModel: list[ModelUsageRow]
    granularity: str


class ObservabilityRunView(BaseModel):
    """One run-list row projected from a trace summary. ``inputPreview``/
    ``outputPreview`` are the backend's server-bounded (structurally-clipped) JSON
    previews; the full bodies live on the trace. Nullable aggregates are ``null``
    when the backend returned none."""

    id: str
    traceId: str
    createdAt: str | None = None
    tags: list[str]
    status: str
    cost: float | None = None
    latencyMs: int | None = None
    totalTokens: int | None = None
    inputPreview: JsonValue
    outputPreview: JsonValue


class ObservabilityRunsPage(BaseModel):
    """One page of observability runs; ``nextPage`` is ``null`` on the last page."""

    items: list[ObservabilityRunView]
    page: int
    nextPage: int | None = None


class SpanView(BaseModel):
    """One span within a run trace. ``input``/``output``/``usage``/``metadata`` are
    the backend's free-form values (open JSON); ``nodeId`` is read from the span's
    metadata when present."""

    id: str
    parentId: str | None = None
    traceId: str | None = None
    name: str | None = None
    type: str | None = None
    level: str | None = None
    statusMessage: str | None = None
    start: str | None = None
    end: str | None = None
    model: str | None = None
    usage: JsonValue = None
    metadata: JsonValue = None
    input: JsonValue = None
    output: JsonValue = None
    nodeId: str | None = None


class RunTraceView(BaseModel):
    """The single-run detail: the trace's attributes (``input``/``output``/
    ``metadata`` are open JSON) plus every span."""

    traceId: str
    timestamp: str | None = None
    tags: list[str]
    totalCost: float | None = None
    input: JsonValue = None
    output: JsonValue = None
    metadata: JsonValue = None
    spans: list[SpanView]


# --- Plugins ----------------------------------------------------------------


class StudioPluginListing(RootModel[list[StudioPluginManifest]]):
    """The bare-list body of ``list_studio_plugins`` — each installed plugin's
    parsed, validated ``StudioPluginManifest`` (a fixed shape, reused as-is)."""


# --- Resources --------------------------------------------------------------


class ResourceContent(RootModel[str | JsonValue]):
    """The loaded resource body of ``get_resource_by_id`` (both the GET fetch and
    the POST render route): the resource text, OR a media block. The fastmcp media
    members (``Image``/``Audio``/``File``) are not pydantic-v2 JSON-schema-able, so
    the media arm is described as its serialized JSON value (``JsonValue``) rather
    than a media model."""


# --- Runs -------------------------------------------------------------------


class RunView(BaseModel):
    """One platform-runs-index row. ``traceId`` deep-links the observability trace
    (``null`` when the run opened none); ``interactionId`` joins a parked run with
    its resume row (``null`` for a plain run); ``endedAt`` is ``null`` while the run
    is still running."""

    runId: str
    preset: str
    version: int | None = None
    traceId: str | None = None
    user: str | None = None
    session: str | None = None
    interactionId: str | None = None
    outcome: str
    startedAt: str
    endedAt: str | None = None


class RunsPage(BaseModel):
    """One page of the platform runs index; ``nextPage`` is ``null`` on the last
    page."""

    items: list[RunView]
    page: int
    nextPage: int | None = None


class RunsPruneResult(BaseModel):
    """The runs-index prune report. ``skipped`` is present ONLY on a no-op branch
    (store off or retention unset); ``cutoff`` is present ONLY on a real prune (the
    ISO cutoff older than which rows were deleted)."""

    retention_days: int | None = None
    pruned_count: int
    skipped: str | None = None
    cutoff: str | None = None


# --- Sandbox ----------------------------------------------------------------


class SandboxPolicy(BaseModel):
    """The resolved sandbox policy surfaced to external consumers: the network
    ``egress`` ceiling, the ``isolation`` floor, the ``scrub_transcript`` flag and
    the ``durable`` gate. Present regardless of whether a provider is registered."""

    egress: str
    isolation: str
    scrub_transcript: bool
    durable: bool


class SandboxInfo(BaseModel):
    """Sandbox identity plus the always-present resolved ``policy``.
    ``provider``/``module`` are ``null`` and ``sessions`` is 0 when no provider is
    registered (``present`` false)."""

    present: bool
    provider: str | None = None
    module: str | None = None
    sessions: int
    policy: SandboxPolicy


# --- Sub-MCP ----------------------------------------------------------------


class SubMcpMapListing(RootModel[dict[str, RouteConfig]]):
    """The registered sub-MCP apps as a dynamic slug-keyed MAP (not an ``items``
    list): each value is the durable ``RouteConfig`` (``tools`` + ``transport``)."""


class SubMcpRegistrationResult(BaseModel):
    """The register/reload result: the ``slug`` mounted and the ``tools`` it exposes
    on ``transport``."""

    slug: str
    tools: list[str]
    transport: str


class SubMcpRemovalResult(BaseModel):
    """The unregister result: the ``slug`` removed and the ``removed`` flag (always
    true — a slug present nowhere is a 404 instead)."""

    slug: str
    removed: bool


# --- System kinds -----------------------------------------------------------


class SystemKindsListing(RootModel[list[KindStatus]]):
    """The bare-list body of ``list_system_kinds`` — one ``KindStatus`` per
    pluggable kind, reused as-is."""
