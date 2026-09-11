"""Response models for group-A operations (api_keys · conversations · manifest ·
marketplace · presets).

Each model DESCRIBES the inner payload its operation returns today; the route adapter
wraps that payload in the ``{"data": ...}`` success envelope, so nothing here re-declares
the envelope. Bare-list and dynamic-map bodies are NAMED ``RootModel`` subclasses (the
offline emitter registers ``components.schemas`` by ``model.__name__``, so a named class
yields a stable, unique component). Shared cross-seam models (``ApplyResponse``,
``FanoutSummary``, ``OpaqueJson``) live in :mod:`tai42_contract.app.responses` and existing
record models are imported, never redefined here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, JsonValue, RootModel
from tai42_contract.access_control.models import RoleDefinition
from tai42_contract.app.responses import ApplyResponse, FanoutSummary
from tai42_contract.conversations import ConversationRouteCreate, TargetConversationConfig
from tai42_contract.template import TemplatedText
from tai42_contract.versioning.models import DocumentVersion

from tai42_skeleton.conversations.models import ConversationRecord

# ---------------------------------------------------------------------------
# api_keys
# ---------------------------------------------------------------------------


class ScopeUrlMap(RootModel[dict[str, str]]):
    """The scope catalog as a ``{url: scope_id}`` mapping; empty when access control
    is disabled."""


class StringList(RootModel[list[str]]):
    """A bare list of strings — the public-route pins and the marketplace category /
    item-kind vocabularies."""


class ScopeUrlAck(BaseModel):
    """Acknowledges a url mapped onto a scope."""

    scope_id: str
    url: str


class UrlAck(BaseModel):
    """Acknowledges a url-scoped mutation (scope-url removal, public-route pin/unpin)."""

    url: str


class ScopeDeleteResult(BaseModel):
    """A deleted scope with the number of keys the cascade rewrote."""

    scope_id: str
    deleted_keys: int


class RouteMappingRow(BaseModel):
    """One HTTP route joined with its scope mapping and route-registry metadata.
    ``mapped`` is the route's scope id, the public marker, or ``null`` when unmapped;
    ``action`` is its authorization action class, ``null`` when the route carries no
    registry metadata."""

    path: str
    methods: list[str]
    mapped: str | None
    tags: list[str]
    summary: str
    action: str | None


class RouteMappingList(RootModel[list[RouteMappingRow]]):
    """The app's HTTP routes with their scope mappings, sorted by path."""


class TokenPayloadRow(BaseModel):
    """A provisioned key's identity merged with its enforced policy — NEVER key
    material. The policy fields are present only when the key carries a stored policy
    row; the owner claim, when set, rides inside ``policy_data``."""

    user_id: str
    description: str
    scopes: list[str] | None = None
    policy_data: dict[str, Any] | None = None
    condition: TemplatedText | None = None


class TokenPayloadList(RootModel[list[TokenPayloadRow]]):
    """Every provisioned key's identity + policy the caller may see."""


class ApiKeyCreateResult(BaseModel):
    """A freshly minted key. ``api_key`` is the raw ``sk-…`` secret surfaced ONCE — it
    is never stored in plaintext and never returned again; ``key_fingerprint`` is the
    key's immutable per-mint identity a binding pins against."""

    api_key: str
    key_fingerprint: str


class UserUpdateAck(BaseModel):
    """Acknowledges an api-key edit."""

    user_id: str
    updated: bool


class ScopesUpdateAck(BaseModel):
    """Acknowledges a granular scope edit, returning the resulting scope set."""

    user_id: str
    updated: bool
    scopes: list[str]


class RevokeAck(BaseModel):
    """Acknowledges an api-key revocation."""

    user_id: str
    revoked: bool


class ClaimLinkResult(BaseModel):
    """A one-time claim link. ``token`` is the claim secret surfaced ONCE (it rides the
    ``claim_path`` URL fragment and is never stored in plaintext); ``expires_at`` is its
    ISO-8601 expiry."""

    claim_path: str
    token: str
    expires_at: str


class ProviderCapability(BaseModel):
    """Whether one configured identity provider can mint keys."""

    name: str
    mintable: bool


class MintCapabilities(BaseModel):
    """Whether any configured identity provider can mint keys, per provider."""

    mintable: bool
    providers: list[ProviderCapability]


class RoleDefinitionList(RootModel[list[RoleDefinition]]):
    """The seeded / operator-authored roles as full role bodies."""


class ConditionCheckResult(BaseModel):
    """A jq policy-condition validation verdict. ``result`` is the sampled allow/deny
    boolean, or ``null`` when no sample context was evaluated."""

    ok: bool
    result: bool | None


class DocumentVersionList(RootModel[list[DocumentVersion]]):
    """A document's append-only version history — the policy history and the preset
    version history both serve this shape."""


class PolicyRollbackResult(BaseModel):
    """The version a policy was rolled back to."""

    user_id: str
    active_version: int


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


class ConversationRouteView(ConversationRouteCreate):
    """A stored conversation route as every read serves it: the client-supplied fields
    plus the server-derived ``execution_key_fingerprint``, with ``callback_secret``
    OMITTED — reads always strip it, so the view never advertises a field the wire
    withholds. The one-time ``callback_secret`` is returned separately by the create
    door."""

    execution_key_fingerprint: str


class ConversationRouteListEnvelope(BaseModel):
    """A page of conversation routes (each secret-stripped) with the total count."""

    items: list[ConversationRouteView]
    total: int


class ConversationRouteCreateResult(BaseModel):
    """The created/replaced route. ``callback_secret`` is the api-door callback signing
    secret surfaced ONCE (``null`` for a channel route); ``route`` is the stored view
    with its secret stripped."""

    created: bool
    route_name: str
    route: ConversationRouteView
    callback_secret: str | None


class ConversationRecordView(ConversationRecord):
    """A conversation answer record as a read door serves it. An admin read carries every
    field; the caller-scoped read withholds the route-key's internal detail — ``channel``,
    ``our_identity``, ``provider_message_id``, ``callback_url``, ``error``,
    ``outbound_message_ids`` and ``attempts`` are present only for an admin caller and are
    absent from the caller_view subset."""

    outbound_message_ids: list[str] | None = None
    attempts: int | None = None


class ThreadSummaryRow(BaseModel):
    """One thread's activity summary, drawn from its newest readable record.
    ``last_activity_at`` is the route index's own sort score; ``last_delivery_status`` is
    that record's delivery-status wire string."""

    thread_id: str
    client_address: str
    last_activity_at: float
    message_count: int
    last_delivery_status: str


class ThreadSummaryEnvelope(BaseModel):
    """A page of thread summaries. ``next_page`` is ``null`` on the last page;
    ``truncated`` is ``true`` when a filtered scan spent its budget before the page
    filled."""

    items: list[ThreadSummaryRow]
    total: int
    page: int
    page_size: int
    next_page: int | None
    truncated: bool


class TranscriptEnvelope(BaseModel):
    """A page of a thread's records (admin full records or the caller_view subset).
    ``order`` is the direction served; ``next_page`` is ``null`` on the last page."""

    items: list[ConversationRecordView]
    total: int
    page: int
    page_size: int
    next_page: int | None
    order: str
    truncated: bool


class MessageSearchEnvelope(BaseModel):
    """A page of a route's message-search matches (admin-only, so always full records)."""

    items: list[ConversationRecordView]
    total: int
    page: int
    page_size: int
    next_page: int | None
    truncated: bool


class FailedConversationsEnvelope(BaseModel):
    """Every answer record whose delivery ended ``failed`` (admin-only, full records)."""

    items: list[ConversationRecordView]
    total: int


class RouteRemoveResult(BaseModel):
    """A conversation route removal. ``removed`` says whether THIS call removed the
    routing row (``false`` when only an owed index reclamation was completed)."""

    removed: bool
    route_name: str


class ThreadDeleteResult(BaseModel):
    """A thread forget. ``removed`` counts the answer records this call deleted."""

    removed: int
    route_name: str
    thread_id: str


class PersonDeleteResult(BaseModel):
    """A person erase. ``removed`` counts the answer records deleted across the person's
    routes; ``erased`` says whether THIS call removed the person row."""

    person_id: str
    removed: int
    erased: bool


class ThreadMessageAck(BaseModel):
    """The stored id of an operator message sent by hand into a thread."""

    message_id: str
    thread_id: str


class ThreadModeView(BaseModel):
    """A thread's control mode and where it comes from — ``source`` is ``thread`` for a
    per-thread override or ``route`` for the route default."""

    mode: str
    source: str


class ThreadModeSetResult(BaseModel):
    """A per-thread mode override write; ``source`` is always ``thread``."""

    route_name: str
    thread_id: str
    mode: str
    source: str


class ConversationConfigListEnvelope(BaseModel):
    """A page of per-target conversation configs with the total count."""

    items: list[TargetConversationConfig]
    total: int


class ConversationConfigSetResult(BaseModel):
    """The created/replaced per-target conversation config."""

    created: bool
    target_kind: str
    target_name: str
    config: TargetConversationConfig


class ConversationConfigDeleteResult(BaseModel):
    """A per-target conversation config removal."""

    removed: bool
    target_kind: str
    target_name: str


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


class PreservedManifestView(BaseModel):
    """The PRESERVED persisted manifest's MCP section + user tools. The ``mcp`` entries
    carry their raw ``!ENV ${KEY}`` markers intact (unresolved config entries, NOT a
    resolved ``TaiMCPConfig``), so no secret value ever leaves on the wire."""

    mcp: list[dict[str, Any]]
    user_tools: list[str]


class McpEnvRef(BaseModel):
    """One ``!ENV ${VAR[:default]}`` marker ref carried by the manifest's MCP section —
    NAMES and BOOLEANS only. ``pointer`` is the marker leaf's json-pointer; ``set`` is
    whether the var is present in the effective environment."""

    var: str
    pointer: str
    has_default: bool
    set: bool


class McpEnvRefList(RootModel[list[McpEnvRef]]):
    """The manifest MCP section's ``!ENV`` marker refs, in document order."""


class FailedMcp(BaseModel):
    """An MCP server skipped by the viability check — its title and a coarse status."""

    title: str
    status: str


class McpHealth(BaseModel):
    """One MCP server's passive dispatch health. ``last_error`` carries a
    ``{type, message, at}`` block when a failure has been recorded, else ``null``;
    ``last_success`` / ``failing_since`` are ISO-8601 timestamps or ``null``."""

    last_success: str | None
    last_error: dict[str, str] | None
    consecutive_failures: int
    failing_since: str | None


class McpStatusSnapshot(BaseModel):
    """The live MCP-binding snapshot: ``bound`` maps each server title to its bound tool
    names, ``failed`` lists the skipped servers, and ``health`` maps every bound and
    failed title to its dispatch-health block."""

    bound: dict[str, list[str]]
    failed: list[FailedMcp]
    health: dict[str, McpHealth]


# ---------------------------------------------------------------------------
# marketplace
# ---------------------------------------------------------------------------


class CompatStatus(BaseModel):
    """An installed distribution's compatibility verdict against the running contract."""

    status: str
    reason: str | None


class ItemRef(BaseModel):
    """One item a plugin's stored spec provides."""

    kind: str
    name: str


class InstalledRow(BaseModel):
    """One installed plugin with its update picture computed against the running
    contract. ``installed_at`` is ISO-8601; ``latest`` / ``incompatible_newer`` are
    ``null`` when none applies; ``delivery`` is ``package`` or ``descriptor``;
    ``route_mounts`` maps each route-carrying item name to its mounted base."""

    ref: str
    version: str
    source: str
    delivery: str
    installed_at: str
    latest: str | None
    update_available: bool
    incompatible_newer: str | None
    missing_upstream: bool
    compat: CompatStatus
    items: list[ItemRef]
    route_mounts: dict[str, str]


class QuarantinedPlugin(BaseModel):
    """A plugin the boot pass SKIPPED (incompatible or import-broken) with its reason."""

    name: str
    reason: str


class InstalledInventory(BaseModel):
    """The installed inventory plus the boot-quarantined plugins."""

    installed: list[InstalledRow]
    quarantined: list[QuarantinedPlugin]


class AdvisorySnapshot(BaseModel):
    """The advisory snapshot for the installed plugins. Each advisory row is an upstream
    registry object forwarded verbatim (its shape is owned by the registry);
    ``fetched_at`` is the ISO-8601 fetch time."""

    advisories: list[dict[str, JsonValue]]
    fetched_at: str


class MountedRoute(BaseModel):
    """One route an install/update mounted."""

    item: str
    full_path: str
    methods: list[str]
    public: bool


class RouteCollision(BaseModel):
    """A declared route that clashes with an already-owned one."""

    item: str
    full_path: str
    methods: list[str]
    conflict_owner: str
    conflict_path: str


class PublicRouteRow(BaseModel):
    """A public route as an acceptance row."""

    item: str
    full_path: str
    methods: list[str]


class RequiredEnvVar(BaseModel):
    """An env var a plugin spec requires, with its derived secret-ness."""

    name: str
    secret: bool


class PreviewItemRoute(BaseModel):
    """One resolved route of a preview's route-carrying item."""

    path: str
    full_path: str
    methods: list[str]
    public: bool


class PreviewItem(BaseModel):
    """One route-carrying item in an install/update preview, with its resolved and
    declared bases and its routes."""

    item: str
    kind: str
    base: str
    default_base: str
    routes: list[PreviewItemRoute]


class InstallResult(BaseModel):
    """The install (and update) receipt. ``package`` is ``null`` for a descriptor-only
    plugin; ``advisories`` are the target's upstream advisory rows forwarded verbatim;
    ``notes`` are activation notes; ``reload`` is the manifest apply's fleet result;
    ``pip_output`` is ``null`` for a descriptor-only plugin; ``routes`` lists every route
    the operation mounted."""

    ref: str
    version: str
    package: str | None
    advisories: list[JsonValue]
    notes: list[str]
    reload: ApplyResponse
    pip_output: str | None
    routes: list[MountedRoute]


class InstallPreview(BaseModel):
    """A no-side-effect install/update preview: the resolved routes per item, the
    collisions against the live registry, the public rows requiring acceptance, the
    ``new_public_routes`` an update has not already approved, the required and missing
    env vars, and the delivery form."""

    ref: str
    version: str
    items: list[PreviewItem]
    collisions: list[RouteCollision]
    public_routes: list[PublicRouteRow]
    new_public_routes: list[PublicRouteRow]
    requires_public_acceptance: bool
    required_env: list[RequiredEnvVar]
    missing_env: list[str]
    delivery: str


class UninstallResult(BaseModel):
    """The uninstall receipt. ``reload`` is ``null`` when the plugin wrote no manifest
    entry to remove; ``notes`` are removal notes (e.g. orphaned env vars left in place)."""

    ref: str
    uninstalled: bool
    reload: ApplyResponse | None
    notes: list[str]


class UpgradeRow(BaseModel):
    """One ref's upgrade-all outcome. ``outcome`` is one of ``upgraded`` / ``up-to-date``
    / ``no-compatible-version`` / ``failed``; ``detail`` is its human-readable note."""

    ref: str
    outcome: str
    detail: str


class UpgradeAllResult(BaseModel):
    """The complete per-ref upgrade-all report."""

    results: list[UpgradeRow]


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------


class PresetRecordView(BaseModel):
    """A preset's record row. ``extensions`` is the ordered extension combos;
    ``conflicted`` marks a quarantined record with its ``conflicted_reason`` (``null``
    when clean); ``uses`` / ``used_by`` are the sorted other presets this body composes
    and that compose it."""

    name: str
    base_tool: str
    description: str
    active_version: int
    extensions: list[list[str | dict[str, Any]]]
    output_schema: dict[str, Any] | None
    input_schema: dict[str, Any] | None
    conflicted: bool
    conflicted_reason: str | None
    uses: list[str]
    used_by: list[str]


class PresetRecordList(RootModel[list[PresetRecordView]]):
    """One row per store-backed preset record, including conflicted rows."""


class PresetDetailView(PresetRecordView):
    """A single preset's record plus its active ``fixed_kwargs``."""

    fixed_kwargs: dict[str, Any]


class PresetCreateResult(PresetRecordView):
    """A created preset's record with the per-worker rebind fan-out report."""

    fanout: FanoutSummary


class PresetVersionSaveResult(DocumentVersion):
    """A newly saved preset version row with the per-worker rebind fan-out report
    (fields merged, not nested)."""

    fanout: FanoutSummary


class PresetRollbackResult(BaseModel):
    """The version a preset was rolled back to, with the rebind fan-out report."""

    name: str
    active_version: int
    fanout: FanoutSummary


class PresetRenameResult(BaseModel):
    """A renamed preset. ``name`` is the new name and ``renamed_from`` the old one; the
    fan-out report is the new binding's propagation."""

    name: str
    renamed_from: str
    active_version: int
    fanout: FanoutSummary


class PresetDeleteResult(BaseModel):
    """A deleted preset with the per-worker removal fan-out report."""

    name: str
    deleted: bool
    fanout: FanoutSummary


class PresetRefereesResult(BaseModel):
    """Every live reference a rename of this preset would strand."""

    name: str
    referees: list[str]


class PresetValidateVerdict(BaseModel):
    """A preset draft validation verdict — ``valid`` is the absence of an ``error``
    (both outcomes answer 200)."""

    valid: bool
    error: str | None


class PresetVersionTagsResult(BaseModel):
    """A preset version's replaced tag annotation."""

    name: str
    version: int
    tags: list[str]
