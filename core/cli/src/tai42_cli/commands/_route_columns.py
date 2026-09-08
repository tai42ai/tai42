"""Generated route->column shapes for the CLI's table renderer.

DO NOT EDIT BY HAND. Each entry is derived from a route's declared response model
by ``core/skeleton/tests/cli/_gen_route_columns.py``; the drift gate in
``core/skeleton/tests/cli/test_route_columns.py`` fails if this table falls out of
sync with the models. Regenerate with ``python core/skeleton/tests/cli/_gen_route_columns.py``.

``emit_records`` reads a route's shape here: ``items_key`` is the envelope's list
field (``None`` when the body itself is the list) and ``columns`` are the row
model's field names in declaration order (``("value",)`` for a bare-scalar row).
"""

from __future__ import annotations

from typing import NamedTuple


class RouteShape(NamedTuple):
    items_key: str | None
    columns: tuple[str, ...]


ROUTE_TABLE_SHAPES: dict[tuple[str, str], RouteShape] = {
    ("DELETE", "/api/connectors/connections/{connection_id}"): RouteShape(
        items_key=None,
        columns=(
            "connection_id",
            "upstream_revoke_outcome",
            "upstream_revoke_status",
            "removed_manifest_entries",
            "fanout",
        ),
    ),
    ("GET", "/api/agents"): RouteShape(
        items_key="items", columns=("name", "description", "tool_name", "input_schema", "spec_runnable")
    ),
    ("GET", "/api/agents/spec-runnable"): RouteShape(
        items_key="items", columns=("name", "description", "tool_name", "input_schema", "spec_runnable")
    ),
    ("GET", "/api/auth/api-keys/{user_id}/policy/versions"): RouteShape(
        items_key=None, columns=("version", "body", "tags", "created_at", "is_current")
    ),
    ("GET", "/api/auth/capabilities"): RouteShape(items_key="providers", columns=("name", "mintable")),
    ("GET", "/api/auth/public-routes"): RouteShape(items_key=None, columns=("value",)),
    ("GET", "/api/auth/roles"): RouteShape(
        items_key=None,
        columns=(
            "condition",
            "condition_id",
            "condition_kwargs",
            "name",
            "description",
            "scopes",
            "base_tier",
            "allow_all",
            "grants",
        ),
    ),
    ("GET", "/api/auth/routes"): RouteShape(
        items_key=None, columns=("path", "methods", "mapped", "tags", "summary", "action")
    ),
    ("GET", "/api/auth/tokens-payload"): RouteShape(
        items_key=None,
        columns=("user_id", "description", "scopes", "policy_data", "condition", "condition_id", "condition_kwargs"),
    ),
    ("GET", "/api/backup/sections"): RouteShape(items_key=None, columns=("name", "secret")),
    ("GET", "/api/channels"): RouteShape(items_key="channels", columns=("value",)),
    ("GET", "/api/config/env"): RouteShape(items_key="secret_keys", columns=("value",)),
    ("GET", "/api/config/profiles"): RouteShape(items_key=None, columns=("name", "description")),
    ("GET", "/api/config/profiles/{name}"): RouteShape(items_key=None, columns=("description", "env", "secret_keys")),
    ("GET", "/api/config/profiles/{name}/versions"): RouteShape(
        items_key=None, columns=("version", "tags", "created_at", "is_current")
    ),
    ("GET", "/api/config/profiles/{name}/versions/{version}"): RouteShape(
        items_key=None, columns=("version", "tags", "created_at", "is_current", "body")
    ),
    ("GET", "/api/config/settings-schema"): RouteShape(
        items_key="groups", columns=("name", "module", "qualname", "fields")
    ),
    ("GET", "/api/connectors/connections"): RouteShape(
        items_key="items",
        columns=(
            "connection_id",
            "provider_id",
            "kind",
            "alias",
            "account_identity",
            "auth_health_state",
            "enabled_sub_services",
            "granted_scopes",
            "unreachable_sub_services",
            "created_at",
        ),
    ),
    ("GET", "/api/conversation-configs"): RouteShape(
        items_key="items", columns=("target_kind", "target_name", "multichannel", "greeting_template")
    ),
    ("GET", "/api/conversations"): RouteShape(
        items_key="items",
        columns=(
            "route_name",
            "door",
            "target_kind",
            "target_name",
            "payload_expr",
            "reply_expr",
            "initial_mode",
            "execution_key",
            "channel",
            "our_identity",
            "callback_url",
            "turns_per_hour_override",
            "error_reply_text",
            "execution_key_fingerprint",
        ),
    ),
    ("GET", "/api/conversations/messages/failed"): RouteShape(
        items_key="items",
        columns=(
            "message_id",
            "route_name",
            "door",
            "thread_id",
            "client_address",
            "channel",
            "our_identity",
            "provider_message_id",
            "callback_url",
            "caller_principal",
            "origin",
            "inbound_text",
            "inbound_form",
            "inbound_attachments",
            "inbound_location",
            "inbound_locale",
            "inbound_kind",
            "inbound_event",
            "submitted_by",
            "answer_status",
            "answer",
            "answer_parts",
            "error",
            "delivery_status",
            "outbound_message_ids",
            "attempts",
            "created_at",
            "updated_at",
        ),
    ),
    ("GET", "/api/conversations/persons/{person_id}"): RouteShape(
        items_key="addresses", columns=("door", "routes", "channel", "our_identity", "address", "linked_at")
    ),
    ("GET", "/api/conversations/{route_name}/messages/search"): RouteShape(
        items_key="items",
        columns=(
            "message_id",
            "route_name",
            "door",
            "thread_id",
            "client_address",
            "channel",
            "our_identity",
            "provider_message_id",
            "callback_url",
            "caller_principal",
            "origin",
            "inbound_text",
            "inbound_form",
            "inbound_attachments",
            "inbound_location",
            "inbound_locale",
            "inbound_kind",
            "inbound_event",
            "submitted_by",
            "answer_status",
            "answer",
            "answer_parts",
            "error",
            "delivery_status",
            "outbound_message_ids",
            "attempts",
            "created_at",
            "updated_at",
        ),
    ),
    ("GET", "/api/conversations/{route_name}/threads"): RouteShape(
        items_key="items",
        columns=("thread_id", "client_address", "last_activity_at", "message_count", "last_delivery_status"),
    ),
    ("GET", "/api/conversations/{route_name}/transcript"): RouteShape(
        items_key="items",
        columns=(
            "message_id",
            "route_name",
            "door",
            "thread_id",
            "client_address",
            "channel",
            "our_identity",
            "provider_message_id",
            "callback_url",
            "caller_principal",
            "origin",
            "inbound_text",
            "inbound_form",
            "inbound_attachments",
            "inbound_location",
            "inbound_locale",
            "inbound_kind",
            "inbound_event",
            "submitted_by",
            "answer_status",
            "answer",
            "answer_parts",
            "error",
            "delivery_status",
            "outbound_message_ids",
            "attempts",
            "created_at",
            "updated_at",
        ),
    ),
    ("GET", "/api/extensions"): RouteShape(items_key=None, columns=("name", "kind")),
    ("GET", "/api/fleet/workers"): RouteShape(
        items_key="workers",
        columns=("name", "kind", "pid", "generation", "joined_at", "beat_at", "state", "stale", "last_op"),
    ),
    ("GET", "/api/hooks"): RouteShape(
        items_key="items",
        columns=(
            "expr",
            "expr_id",
            "expr_kwargs",
            "condition",
            "condition_id",
            "condition_kwargs",
            "name",
            "topic",
            "tool",
            "execution_key",
            "tool_kwargs",
            "subject",
            "execution_key_fingerprint",
        ),
    ),
    ("GET", "/api/hooks/trigger-links"): RouteShape(
        items_key="items",
        columns=(
            "name",
            "topic",
            "execution_key",
            "execution_key_fingerprint",
            "require_api_key",
            "tool_kwargs",
            "created_by",
            "created_at",
            "expires_at",
            "token_hash_prefix",
            "trigger_auth",
        ),
    ),
    ("GET", "/api/hooks/verifiers"): RouteShape(items_key=None, columns=("value",)),
    ("GET", "/api/interactions"): RouteShape(
        items_key="items",
        columns=(
            "interaction_id",
            "group_id",
            "question",
            "answer_format",
            "format_payload",
            "created_at",
            "timeout_at",
            "sensitive",
            "server_verified",
            "channel",
            "recipient",
            "origin",
            "audience",
            "media",
        ),
    ),
    ("GET", "/api/interactions/pending"): RouteShape(
        items_key="items",
        columns=(
            "interaction_id",
            "group_id",
            "question",
            "channel",
            "recipient",
            "audience",
            "thread_id",
            "expiry_at",
            "created_at",
            "mode",
        ),
    ),
    ("GET", "/api/manifest/mcp-env-refs"): RouteShape(items_key=None, columns=("var", "pointer", "has_default", "set")),
    ("GET", "/api/marketplace/categories"): RouteShape(items_key=None, columns=("value",)),
    ("GET", "/api/marketplace/kinds"): RouteShape(items_key=None, columns=("value",)),
    ("GET", "/api/mcp-status"): RouteShape(items_key="failed", columns=("title", "status")),
    ("GET", "/api/mcp-status/failed"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("GET", "/api/notifications"): RouteShape(
        items_key="notifications",
        columns=(
            "id",
            "message",
            "recipient",
            "audience",
            "media",
            "template",
            "options",
            "location",
            "sections",
            "header",
            "footer",
            "schema",
            "created_at",
        ),
    ),
    ("GET", "/api/observability/runs"): RouteShape(
        items_key="items",
        columns=(
            "id",
            "traceId",
            "createdAt",
            "tags",
            "status",
            "cost",
            "latencyMs",
            "totalTokens",
            "inputPreview",
            "outputPreview",
        ),
    ),
    ("GET", "/api/plugins"): RouteShape(
        items_key=None, columns=("name", "version", "api_version", "entry", "integrity", "contributions")
    ),
    ("GET", "/api/presets"): RouteShape(
        items_key=None,
        columns=(
            "name",
            "base_tool",
            "description",
            "active_version",
            "extensions",
            "output_schema",
            "input_schema",
            "conflicted",
            "conflicted_reason",
            "uses",
            "used_by",
        ),
    ),
    ("GET", "/api/presets/{name}/referees"): RouteShape(items_key=None, columns=("name", "referees")),
    ("GET", "/api/presets/{name}/versions"): RouteShape(
        items_key=None, columns=("version", "body", "tags", "created_at", "is_current")
    ),
    ("GET", "/api/presets/{name}/versions/{version}"): RouteShape(
        items_key=None, columns=("version", "body", "tags", "created_at", "is_current")
    ),
    ("GET", "/api/runs"): RouteShape(
        items_key="items",
        columns=(
            "runId",
            "preset",
            "version",
            "traceId",
            "user",
            "session",
            "interactionId",
            "outcome",
            "startedAt",
            "endedAt",
        ),
    ),
    ("GET", "/api/state-modules"): RouteShape(
        items_key=None,
        columns=(
            "kind",
            "name",
            "description",
            "parameters",
            "schema_",
            "regimes",
            "declarations",
            "trace",
            "mounted_on",
            "shipped_default",
        ),
    ),
    ("GET", "/api/states"): RouteShape(
        items_key=None,
        columns=(
            "name",
            "description",
            "schema_",
            "subject_kinds",
            "default_subject_kind",
            "retention_days",
            "effective_schema",
            "regimes",
            "updated_at",
        ),
    ),
    ("GET", "/api/states/{name}/consumers"): RouteShape(
        items_key=None, columns=("kind", "name", "detail", "link", "unavailable")
    ),
    ("GET", "/api/states/{name}/mounts"): RouteShape(
        items_key=None, columns=("module", "path", "parameters", "declarations", "state")
    ),
    ("GET", "/api/states/{name}/mounts/{module}"): RouteShape(
        items_key=None, columns=("module", "path", "parameters", "declarations", "state")
    ),
    ("GET", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}/writes"): RouteShape(
        items_key="items", columns=("seq", "at", "origin", "paths")
    ),
    ("GET", "/api/states/{name}/subjects"): RouteShape(items_key="subjects", columns=("subject", "updated_at")),
    ("GET", "/api/storage/resources"): RouteShape(items_key="resources", columns=("value",)),
    ("GET", "/api/system/kinds"): RouteShape(items_key=None, columns=("kind", "state", "plugin", "detail")),
    ("GET", "/api/templates"): RouteShape(items_key=None, columns=("value",)),
    ("GET", "/api/tool-runs"): RouteShape(
        items_key=None, columns=("run_id", "tool_name", "status", "started_at", "finished_at")
    ),
    ("GET", "/api/tools"): RouteShape(items_key=None, columns=("value",)),
    ("GET", "/api/tools/tags"): RouteShape(items_key=None, columns=("name", "tags", "hidden", "badges")),
    ("PATCH", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"): RouteShape(
        items_key="folded_from", columns=("target_kind", "target_name", "kind", "key")
    ),
    ("POST", "/api/auth/api-keys/{user_id}/scopes"): RouteShape(
        items_key=None, columns=("user_id", "updated", "scopes")
    ),
    ("POST", "/api/auth/roles"): RouteShape(
        items_key=None,
        columns=(
            "condition",
            "condition_id",
            "condition_kwargs",
            "name",
            "description",
            "scopes",
            "base_tier",
            "allow_all",
            "grants",
        ),
    ),
    ("POST", "/api/auth/roles/{name}/grants"): RouteShape(
        items_key=None,
        columns=(
            "condition",
            "condition_id",
            "condition_kwargs",
            "name",
            "description",
            "scopes",
            "base_tier",
            "allow_all",
            "grants",
        ),
    ),
    ("POST", "/api/auth/roles/{name}/rollback"): RouteShape(
        items_key=None,
        columns=(
            "condition",
            "condition_id",
            "condition_kwargs",
            "name",
            "description",
            "scopes",
            "base_tier",
            "allow_all",
            "grants",
        ),
    ),
    ("POST", "/api/checkpoints/sweep"): RouteShape(
        items_key=None, columns=("provider", "ttl_minutes", "swept_count", "swept_threads", "skipped")
    ),
    ("POST", "/api/config/reload"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("POST", "/api/connectors/tokens/reencrypt"): RouteShape(
        items_key=None, columns=("scanned", "reencrypted", "skipped", "failed", "failed_connection_ids", "cas_retries")
    ),
    ("POST", "/api/fleet/reload-config"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("POST", "/api/marketplace/uninstall"): RouteShape(
        items_key=None, columns=("ref", "uninstalled", "reload", "notes")
    ),
    ("POST", "/api/marketplace/upgrade-all"): RouteShape(items_key="results", columns=("ref", "outcome", "detail")),
    ("POST", "/api/mcp-status/reload-failed"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("POST", "/api/mcp-status/{title}/deregister"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("POST", "/api/mcp-status/{title}/reload"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("POST", "/api/presets/{name}/versions"): RouteShape(
        items_key=None, columns=("version", "body", "tags", "created_at", "is_current", "fanout")
    ),
    ("POST", "/api/states/{name}/records/search"): RouteShape(items_key="matches", columns=("subject", "updated_at")),
    ("POST", "/api/sub-mcp"): RouteShape(items_key=None, columns=("slug", "tools", "transport")),
    ("POST", "/api/tools/reload"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("POST", "/api/tools/remove"): RouteShape(
        items_key="results", columns=("name", "outcome", "payload", "error", "detail")
    ),
    ("PUT", "/api/auth/roles/{name}"): RouteShape(
        items_key=None,
        columns=(
            "condition",
            "condition_id",
            "condition_kwargs",
            "name",
            "description",
            "scopes",
            "base_tier",
            "allow_all",
            "grants",
        ),
    ),
    ("PUT", "/api/conversations/persons/{person_id}/locale"): RouteShape(
        items_key="addresses", columns=("door", "routes", "channel", "our_identity", "address", "linked_at")
    ),
    ("PUT", "/api/presets/{name}/versions/{version}/tags"): RouteShape(
        items_key=None, columns=("name", "version", "tags")
    ),
    ("PUT", "/api/states/{name}"): RouteShape(
        items_key=None,
        columns=(
            "name",
            "description",
            "schema_",
            "subject_kinds",
            "default_subject_kind",
            "retention_days",
            "effective_schema",
            "regimes",
            "updated_at",
        ),
    ),
    ("PUT", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"): RouteShape(
        items_key="folded_from", columns=("target_kind", "target_name", "kind", "key")
    ),
}
