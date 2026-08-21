"""OpenAPI 3.1 emitter tests — the coverage gate, offline emission, and the
``tai openapi`` command.

The coverage gate is the contract enforcer: every registered ``/api/*`` route
must appear in the emitted spec, meet the self-describe minimum bar, carry the
reload-gate ``503`` when it is gated, and the whole document must validate
against the OpenAPI 3.1 schema. A route that fails to self-describe fails here.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator, ValidationError
from openapi_spec_validator import validate
from pydantic import BaseModel, Field
from tai42_cli import app as app_module

from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.app.route_registry import RouteAction, RouteMetadata, load_api_routes, method_to_action
from tai42_skeleton.cli.openapi import (
    _STATUS_DESCRIPTIONS,
    _assign_component,
    _openapi_path,
    _query_parameters,
    _register_model,
    build_openapi_spec,
)
from tai42_skeleton.cli.openapi import _operation as _emit_operation
from tai42_skeleton.operations.conversations import MAX_THREAD_PAGE


@pytest.fixture(scope="module")
def spec() -> dict:
    return build_openapi_spec()


@pytest.fixture(scope="module")
def api_routes() -> list[RouteMetadata]:
    return load_api_routes()


def _operation(spec: dict, meta: RouteMetadata, method: str) -> dict:
    # Normalize the Starlette path to its OpenAPI form exactly as the emitter does
    # (dropping the ``:path`` converter from any param), so a ``{name:path}`` route
    # is matched however it is named.
    oapath = _openapi_path(meta.path)
    assert oapath in spec["paths"], f"route {meta.path} missing from spec"
    op = spec["paths"][oapath].get(method.lower())
    assert op is not None, f"{method} {meta.path} missing from spec"
    return op


# -- Coverage gate -----------------------------------------------------------


def test_every_api_route_appears_in_the_spec(spec: dict, api_routes: list[RouteMetadata]) -> None:
    assert api_routes, "no /api routes enumerated — the registry is empty"
    for meta in api_routes:
        for method in meta.methods:
            _operation(spec, meta, method)


def test_every_route_meets_the_self_describe_bar(api_routes: list[RouteMetadata]) -> None:
    for meta in api_routes:
        assert meta.summary, f"{meta.path} missing a summary"
        assert meta.tags, f"{meta.path} missing tags"
        assert isinstance(meta.authed, bool), f"{meta.path} has a non-bool authed"
        # request_model is REQUIRED for every authed route that reads a body; a
        # public external door (authed=False) may accept an opaque provider body.
        if meta.authed and meta.reads_body:
            assert meta.request_model is not None, f"{meta.path} reads a body but declares no request_model"
        # response_model is always present as an attribute; None IS the accepted
        # opaque marker, so its mere presence is the bar (never AttributeError).
        assert meta.response_model is None or isinstance(meta.response_model, type)


def test_gated_routes_declare_the_retriable_503(spec: dict, api_routes: list[RouteMetadata]) -> None:
    gated = [m for m in api_routes if m.reload_gated]
    assert gated, "no reload-gated routes derived — the reload-gate derivation is broken"
    for meta in gated:
        for method in meta.methods:
            op = _operation(spec, meta, method)
            assert "503" in op["responses"], f"gated route {method} {meta.path} lacks the 503 response"
            response = op["responses"]["503"]
            assert response["headers"]["Retry-After"]["schema"]["type"] == "integer"


# -- The two sources of a 503, and the SHAPE each publishes -------------------
#
# A 503 reaches a route from two independent sources that answer DIFFERENT bodies, so
# the published shape has to follow the source rather than the status:
#
# * the reload gate (``reload_gated``) answers the constant-message ``ReloadingError``
#   body plus a ``Retry-After`` header;
# * a declared ``503`` (an operation's ``UnavailableError``, or a native handler that
#   declares one) answers the plain ``{"error": <message>}`` envelope with no
#   ``reloading`` field and no header.
#
# Every route publishing a 503 is pinned into its class below, so a route changing
# source — or a new one arriving — trips here and its shape is re-confirmed.
_EXPECTED_503_GATE_ONLY: set[tuple[str, str]] = {
    ("DELETE", "/api/agents-config/entries/{title}"),
    ("DELETE", "/api/mcp-config/entries/{title}"),
    ("DELETE", "/api/presets/{name}"),
    ("DELETE", "/api/sub-mcp/{slug}"),
    ("DELETE", "/api/tools-config/entries/{title}"),
    ("POST", "/api/agents-config/entries"),
    ("POST", "/api/agents/authored/{name}/runs"),
    ("POST", "/api/agents/{name}/runs"),
    ("POST", "/api/api-tools"),
    ("POST", "/api/backup/import"),
    ("POST", "/api/checkpoints/sweep"),
    ("POST", "/api/config/env"),
    ("POST", "/api/config/profiles/{name}/apply"),
    ("POST", "/api/config/reload"),
    ("POST", "/api/manifest/replace"),
    ("POST", "/api/mcp-config"),
    ("POST", "/api/mcp-config/entries"),
    ("POST", "/api/mcp-config/secret-env"),
    ("POST", "/api/mcp-status/reload-failed"),
    ("POST", "/api/mcp-status/{title}/deregister"),
    ("POST", "/api/mcp-status/{title}/reload"),
    # ``/api/presets`` create refuses a store-less deploy with a 501 (NotSupportedError),
    # not a declared 503, so only the reload gate's 503 remains here.
    ("POST", "/api/presets"),
    ("POST", "/api/presets/{name}/rename"),
    ("POST", "/api/presets/{name}/rollback"),
    ("POST", "/api/presets/{name}/versions"),
    ("POST", "/api/run-tool"),
    ("POST", "/api/sub-mcp"),
    ("POST", "/api/tools-config/entries"),
    ("POST", "/api/tools/reload"),
    ("POST", "/api/tools/remove"),
    ("POST", "/api/tools/{name}/extensions"),
    ("POST", "/api/tools/{name}/extensions/combos"),
}

# The versioning store-unconfigured refusal is a 501, not a 503, so ``validate`` and
# the version-tags PUT (neither reload-gated) declare NO 503 at all and are absent
# from this set.
_EXPECTED_503_DECLARED_ONLY: set[tuple[str, str]] = {
    # The operator thread/person doors — the forget-thread DELETE, the erase-person DELETE and
    # the operator-send POST — declare the per-thread FIFO's full-queue UnavailableError
    # (retriable), and none is reload-gated, so each carries that 503 alone.
    ("DELETE", "/api/conversations/{route_name}/thread"),
    ("DELETE", "/api/conversations/persons/{person_id}"),
    ("GET", "/api/schedules"),
    ("GET", "/api/schedules/server-datetime"),
    ("POST", "/api/conversations/{route_name}/thread/messages"),
    # The send door declares the transient channel-delivery UnavailableError (503,
    # retryable, carrying the medium's retry_after); it is not reload-gated, so it
    # carries that 503 alone.
    ("POST", "/api/notifications"),
}

_EXPECTED_503_BOTH: set[tuple[str, str]] = {
    ("DELETE", "/api/schedules/{schedule_name}"),
    ("POST", "/api/conversations/{route_name}/messages"),
    # The marketplace mutations keep a declared 503 beside the reload gate: it is the
    # transient fleet-advisory-lock UnavailableError (retriable), NOT the store gate,
    # which refuses with a 501. Both sources coexist here alongside that 501.
    ("POST", "/api/marketplace/install"),
    ("POST", "/api/marketplace/uninstall"),
    ("POST", "/api/marketplace/update"),
    ("POST", "/api/marketplace/upgrade-all"),
    ("POST", "/api/schedules"),
    # tool-runs keeps its declared 503: it is the per-worker CAPACITY UnavailableError
    # (retry later), independent of the store gate, which is a 501.
    ("POST", "/api/tool-runs"),
}


def _503_source_classes(api_routes: list[RouteMetadata]) -> dict[str, set[tuple[str, str]]]:
    classes: dict[str, set[tuple[str, str]]] = {"gate": set(), "declared": set(), "both": set()}
    for meta in api_routes:
        declared = 503 in meta.error_statuses
        if meta.reload_gated and declared:
            key = "both"
        elif meta.reload_gated:
            key = "gate"
        elif declared:
            key = "declared"
        else:
            continue
        classes[key].update((method, meta.path) for method in meta.methods)
    return classes


def test_503_source_classes_match_ground_truth(api_routes: list[RouteMetadata]) -> None:
    classes = _503_source_classes(api_routes)
    assert classes["gate"] == _EXPECTED_503_GATE_ONLY
    assert classes["declared"] == _EXPECTED_503_DECLARED_ONLY
    assert classes["both"] == _EXPECTED_503_BOTH


@pytest.mark.parametrize(("method", "path"), sorted(_EXPECTED_503_GATE_ONLY))
def test_gate_only_503_publishes_the_reloading_shape(spec: dict, method: str, path: str) -> None:
    # Reload gate alone: the constant-message reloading body, and the Retry-After header
    # the gate always stamps.
    response = spec["paths"][path][method.lower()]["responses"]["503"]
    assert response["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ReloadingError"}
    assert response["headers"]["Retry-After"]["schema"] == {"type": "integer"}


@pytest.mark.parametrize(("method", "path"), sorted(_EXPECTED_503_DECLARED_ONLY))
def test_declared_only_503_publishes_the_plain_error_shape(spec: dict, method: str, path: str) -> None:
    # No reload gate on these doors: the adapter renders ``{"error": <message>}`` with no
    # ``reloading`` field and no ``Retry-After``, so neither may be published.
    response = spec["paths"][path][method.lower()]["responses"]["503"]
    assert response["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}
    assert "headers" not in response
    assert response["description"] == _STATUS_DESCRIPTIONS[503]


@pytest.mark.parametrize(("method", "path"), sorted(_EXPECTED_503_BOTH))
def test_both_sources_503_publishes_a_schema_admitting_either_body(spec: dict, method: str, path: str) -> None:
    # One 503 slot, two possible bodies: the schema must admit both, and the header is
    # published because the reloading half carries it.
    response = spec["paths"][path][method.lower()]["responses"]["503"]
    schema = response["content"]["application/json"]["schema"]
    assert schema == {
        "anyOf": [
            {"$ref": "#/components/schemas/ReloadingError"},
            {"$ref": "#/components/schemas/Error"},
        ]
    }
    assert response["headers"]["Retry-After"]["schema"] == {"type": "integer"}


def test_the_combined_503_schema_admits_both_bodies_and_oneof_would_not(spec: dict) -> None:
    """The combined shape is ``anyOf`` for a reason, pinned by validating real bodies.

    ``Error`` constrains only ``error``, so a reloading body satisfies BOTH branches.
    Under ``anyOf`` each of the two real bodies validates; under ``oneOf`` — exactly one
    branch — the gate's own response would fail the spec it is published under.
    """
    combined = spec["paths"]["/api/schedules"]["post"]["responses"]["503"]["content"]["application/json"]["schema"]
    # The published component schemas ride along so the document-relative ``$ref``s in the
    # combined schema resolve exactly as they do in the spec.
    components = {"components": spec["components"]}
    reloading_body = {"error": REJECT_MESSAGE, "reloading": True}
    unavailable_body = {"error": "no installed backend exposes scheduling tools"}

    any_of = Draft202012Validator({**combined, **components})
    any_of.validate(reloading_body)
    any_of.validate(unavailable_body)

    one_of = Draft202012Validator({"oneOf": combined["anyOf"], **components})
    with pytest.raises(ValidationError):
        one_of.validate(reloading_body)


# The reload-gated routes, hand-maintained as ground truth. ``reload_gated`` is
# DECLARED per route (an operation's metadata, or a native handler's explicit
# declaration). Pinning the declared set to this list turns any change to the
# gated surface into a test failure, forcing the author to confirm the 503
# coverage.
_EXPECTED_RELOAD_GATED: set[tuple[str, str]] = {
    ("POST", "/api/agents-config/entries"),
    ("DELETE", "/api/agents-config/entries/{title}"),
    ("POST", "/api/agents/authored/{name}/runs"),
    ("POST", "/api/agents/{name}/runs"),
    ("POST", "/api/api-tools"),
    ("POST", "/api/backup/import"),
    ("POST", "/api/checkpoints/sweep"),
    ("POST", "/api/config/env"),
    ("POST", "/api/config/profiles/{name}/apply"),
    ("POST", "/api/config/reload"),
    ("POST", "/api/conversations/{route_name}/messages"),
    ("POST", "/api/manifest/replace"),
    ("POST", "/api/marketplace/install"),
    ("POST", "/api/marketplace/uninstall"),
    ("POST", "/api/marketplace/update"),
    ("POST", "/api/marketplace/upgrade-all"),
    ("POST", "/api/mcp-config"),
    ("POST", "/api/mcp-config/entries"),
    ("DELETE", "/api/mcp-config/entries/{title}"),
    ("POST", "/api/mcp-config/secret-env"),
    ("POST", "/api/mcp-status/reload-failed"),
    ("POST", "/api/mcp-status/{title}/deregister"),
    ("POST", "/api/mcp-status/{title}/reload"),
    ("POST", "/api/presets"),
    ("DELETE", "/api/presets/{name}"),
    ("POST", "/api/presets/{name}/rename"),
    ("POST", "/api/presets/{name}/rollback"),
    ("POST", "/api/presets/{name}/versions"),
    ("POST", "/api/run-tool"),
    ("POST", "/api/schedules"),
    ("DELETE", "/api/schedules/{schedule_name}"),
    ("POST", "/api/sub-mcp"),
    ("DELETE", "/api/sub-mcp/{slug}"),
    ("POST", "/api/tool-runs"),
    ("POST", "/api/tools-config/entries"),
    ("DELETE", "/api/tools-config/entries/{title}"),
    ("POST", "/api/tools/reload"),
    ("POST", "/api/tools/remove"),
    ("POST", "/api/tools/{name}/extensions"),
    ("POST", "/api/tools/{name}/extensions/combos"),
}

# The body-reading routes, hand-maintained as ground truth (same coverage intent as
# the gated set: ``reads_body`` is declared per route, so pinning the full set trips
# on any change to the body-reading surface).
_EXPECTED_READS_BODY: set[tuple[str, str]] = {
    ("POST", "/api/agents-config/entries"),
    ("POST", "/api/agents/authored/{name}/runs"),
    ("POST", "/api/agents/{name}/runs"),
    ("POST", "/api/api-tools"),
    ("POST", "/api/auth/api-keys"),
    ("PUT", "/api/auth/api-keys/{user_id}"),
    ("POST", "/api/auth/api-keys/{user_id}/policy/rollback"),
    ("POST", "/api/auth/api-keys/{user_id}/scopes"),
    ("POST", "/api/auth/claim-links"),
    ("POST", "/api/auth/roles"),
    ("PUT", "/api/auth/roles/{name}"),
    ("POST", "/api/auth/roles/{name}/grants"),
    ("POST", "/api/auth/roles/{name}/rollback"),
    ("POST", "/api/auth/scopes"),
    ("DELETE", "/api/auth/scopes/urls"),
    ("POST", "/api/auth/public-routes"),
    ("DELETE", "/api/auth/public-routes"),
    ("POST", "/api/auth/validate-condition"),
    ("POST", "/api/fleet/reload-config"),
    ("POST", "/api/backup/export"),
    ("POST", "/api/backup/import"),
    ("POST", "/api/config/env"),
    ("POST", "/api/config/reload"),
    ("PUT", "/api/config/profiles/{name}"),
    ("POST", "/api/config/profiles/{name}/rollback"),
    ("POST", "/api/connectors/connections/start"),
    ("POST", "/api/connectors/connections/{connection_id}/reconnect"),
    ("PATCH", "/api/connectors/connections/{connection_id}/sub-services"),
    ("POST", "/api/connectors/oauth/complete"),
    ("POST", "/api/conversations/{route_name}"),
    ("POST", "/api/conversations/{route_name}/messages"),
    ("PUT", "/api/conversation-configs/{target_kind}/{target_name}"),
    ("POST", "/api/delete-template"),
    ("POST", "/api/delete-template-dir"),
    ("POST", "/api/hooks"),
    ("POST", "/api/hooks/trigger-links"),
    ("PUT", "/api/hooks/topics/{topic}/verifier"),
    ("POST", "/api/interactions/{interaction_id}/answer"),
    ("POST", "/api/login/claim"),
    ("POST", "/api/manifest/replace"),
    ("POST", "/api/marketplace/install"),
    ("POST", "/api/marketplace/install/preview"),
    ("POST", "/api/marketplace/uninstall"),
    ("POST", "/api/marketplace/update"),
    ("POST", "/api/mcp-config"),
    ("POST", "/api/mcp-config/entries"),
    ("POST", "/api/mcp-config/secret-env"),
    ("POST", "/api/mcp-status/reload-failed"),
    ("POST", "/api/mcp-status/{title}/deregister"),
    ("POST", "/api/mcp-status/{title}/reload"),
    ("POST", "/api/notifications"),
    ("POST", "/api/presets"),
    ("POST", "/api/presets/validate"),
    ("POST", "/api/presets/{name}/rename"),
    ("POST", "/api/presets/{name}/rollback"),
    ("POST", "/api/presets/{name}/versions"),
    ("PUT", "/api/presets/{name}/versions/{version}/tags"),
    ("POST", "/api/render-template"),
    ("POST", "/api/resources/get"),
    ("POST", "/api/run-tool"),
    ("POST", "/api/schedules"),
    ("POST", "/api/storage/resources"),
    ("POST", "/api/sub-mcp"),
    ("POST", "/api/template"),
    ("PATCH", "/api/tool-meta/tools/{tool_name}"),
    ("POST", "/api/tool-meta/folders"),
    ("POST", "/api/tool-meta/folders/{folder_id}/move"),
    ("POST", "/api/tool-meta/folders/{folder_id}/rename"),
    ("POST", "/api/tool-runs"),
    ("POST", "/api/tools-config/entries"),
    ("POST", "/api/tools/reload"),
    ("POST", "/api/tools/remove"),
    ("POST", "/api/tools/{name}/extensions"),
    ("POST", "/api/tools/{name}/extensions/combos"),
    ("POST", "/api/upload-template"),
}


def test_declared_reload_gated_set_matches_ground_truth(api_routes: list[RouteMetadata]) -> None:
    declared = {(method, meta.path) for meta in api_routes if meta.reload_gated for method in meta.methods}
    assert declared == _EXPECTED_RELOAD_GATED


def test_declared_reads_body_set_matches_ground_truth(api_routes: list[RouteMetadata]) -> None:
    declared = {(method, meta.path) for meta in api_routes if meta.reads_body for method in meta.methods}
    assert declared == _EXPECTED_READS_BODY


def test_tool_runs_submission_documents_the_202_accepted(spec: dict) -> None:
    responses = spec["paths"]["/api/tool-runs"]["post"]["responses"]
    assert "202" in responses, "the detached tool-run submission returns 202, not 200"
    assert "200" not in responses
    assert responses["202"]["content"]["application/json"]["schema"]["required"] == ["data"]


def test_callback_documents_html_get_and_json_post(spec: dict) -> None:
    # The callback door is one registration with two methods that answer different
    # media types: GET serves the browser confirm page (HTML) while POST is the
    # programmatic answer door returning the ``{"data": ...}`` JSON envelope.
    callback = spec["paths"]["/api/interactions/callback/{ticket}"]
    get_content = callback["get"]["responses"]["200"]["content"]
    assert list(get_content) == ["text/html"]
    post_content = callback["post"]["responses"]["200"]["content"]
    assert list(post_content) == ["application/json"]
    assert post_content["application/json"]["schema"]["required"] == ["data"]


def test_callback_documents_its_error_statuses(spec: dict, api_routes: list[RouteMetadata]) -> None:
    # The callback door declares the full set it answers: 400 (malformed JSON body),
    # 401 (failed verification), 404 (unknown/expired ticket), 413 (oversized
    # body/query), 500 (verifier error). Pinned as ground truth so a change to the
    # declared set trips here.
    (callback,) = [m for m in api_routes if m.path == "/api/interactions/callback/{ticket}"]
    assert set(callback.error_statuses) == {400, 401, 404, 413, 500}
    responses = spec["paths"]["/api/interactions/callback/{ticket}"]["post"]["responses"]
    for status in ("400", "401", "404", "413", "500"):
        assert status in responses, f"callback POST is missing the {status} response"


def test_observability_routes_document_the_501(api_routes: list[RouteMetadata]) -> None:
    # Every observability route answers 501 when monitoring reads are unsupported
    # (MonitoringReadNotSupportedError), so each declares it.
    observability = [m for m in api_routes if m.path.startswith("/api/observability/")]
    assert observability, "no observability routes enumerated"
    for meta in observability:
        assert 501 in meta.error_statuses, f"{meta.path} lost the 501"


# The uniform-gating doors that refuse with a 501 ``NotSupportedError`` (carrying
# a machine-readable ``code``) when their DB-backed feature is OFF — the honest
# refusal a mutation answers on an unconfigured store, and the SSE stream's
# before-body 501. Same declared-error mechanics as the observability 501 precedent:
# ``NotSupportedError`` in the operation's ``errors=`` (or the custom route's
# ``error_statuses``) puts a 501 in the emitted spec, and the ``code`` rides the
# error body exactly as observability's does. Pinned as a subset each door must
# declare, so a lost 501 trips here.
_OFF_GATE_501_DOORS: set[tuple[str, str]] = {
    ("POST", "/api/tool-runs"),
    ("GET", "/api/interactions/stream"),
    ("POST", "/api/notifications"),
    ("POST", "/api/marketplace/install"),
    ("POST", "/api/marketplace/install/preview"),
    ("POST", "/api/marketplace/uninstall"),
    ("POST", "/api/marketplace/update"),
    ("POST", "/api/marketplace/upgrade-all"),
    ("PATCH", "/api/tool-meta/tools/{tool_name}"),
    ("POST", "/api/tool-meta/folders"),
    ("POST", "/api/connectors/connections/start"),
    ("POST", "/api/presets"),
    ("POST", "/api/presets/validate"),
    ("PUT", "/api/presets/{name}/versions/{version}/tags"),
    ("POST", "/api/auth/api-keys"),
    ("PUT", "/api/auth/api-keys/{user_id}"),
    ("DELETE", "/api/auth/api-keys/{user_id}"),
    # The residual access-control mutations gate OFF identically when
    # ACCESS_CONTROL_ENABLE=false — scope/public-route/claim/policy writes all refuse
    # with the same 501 rather than operate the store under the synthetic admin.
    ("POST", "/api/auth/scopes"),
    ("DELETE", "/api/auth/scopes/urls"),
    ("DELETE", "/api/auth/scopes/{scope_id}"),
    ("POST", "/api/auth/public-routes"),
    ("DELETE", "/api/auth/public-routes"),
    ("POST", "/api/auth/claim-links"),
    ("POST", "/api/auth/api-keys/{user_id}/policy/rollback"),
}


@pytest.mark.parametrize(("method", "path"), sorted(_OFF_GATE_501_DOORS))
def test_off_gate_doors_document_the_501(spec: dict, api_routes: list[RouteMetadata], method: str, path: str) -> None:
    # Both halves are pinned: the route declares the 501 in its metadata, and the
    # emitted operation publishes a 501 response — so a dropped OFF gate trips here.
    (meta,) = [m for m in api_routes if m.path == path and method in m.methods]
    assert 501 in meta.error_statuses, f"{method} {path} lost its OFF-gate 501"
    assert "501" in spec["paths"][path][method.lower()]["responses"], f"{method} {path} 501 missing from the spec"


def test_delete_template_declares_only_its_typed_errors(api_routes: list[RouteMetadata]) -> None:
    # delete-template's operation declares BadRequestError only, so the route
    # documents {400, 401} and no spurious 500.
    (meta,) = [m for m in api_routes if m.path == "/api/delete-template"]
    assert set(meta.error_statuses) == {400, 401}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/auth/scopes/urls"),
        ("DELETE", "/api/auth/public-routes"),
    ],
)
def test_scope_url_delete_doors_document_the_400(
    spec: dict, api_routes: list[RouteMetadata], method: str, path: str
) -> None:
    # Both delete doors validate their JSON body at the request edge (a blank/missing
    # ``url`` is a 400) before the operation runs (a url that was never mapped is a 404),
    # and refuse with a 501 when access control is disabled (the OFF gate), so each
    # documents {400, 401, 404, 501} — the 400 the extractor answers must be in the spec,
    # not just the runtime.
    (meta,) = [m for m in api_routes if m.path == path and method in m.methods]
    assert set(meta.error_statuses) == {400, 401, 404, 501}
    responses = spec["paths"][path][method.lower()]["responses"]
    assert "400" in responses, f"{method} {path} is missing the 400 response"


# The five run-any-tool doors and the exact status set each declares, hand-maintained as
# ground truth. All five dispatch a named tool and share one error story: a typed
# ``PermissionDenied`` from under the dispatch passes through (403 on every one) and any
# other raise is enveloped as an ``OperationFailed`` (500 on every one). The sets are the
# statuses each door answers with the plain ``{"error": ...}`` envelope — its ``errors=``
# list plus the 401 the authed flag adds. The reload gate's 503 is NOT one of them (it
# answers a different body); the gated doors among these carry it via ``reload_gated``,
# and the test adds it to the expected SPEC responses from that flag.
#
# Declared is not the same as reachable, and nothing here says a door cannot answer a
# status it leaves undeclared. ``PermissionDenied`` is only the sharpest case of the
# passthrough: the dispatch re-raises EVERY ``OperationError`` the inner tool raises, so
# a tool whose body raises its own ``NotFoundError`` answers 404 through a door whose set
# below holds none. That status belongs to the inner tool, not to the door's contract —
# the equality is over what each door DECLARES, which is what a client reads off the spec.
_EXPECTED_TOOL_DISPATCH_DOOR_STATUSES: dict[tuple[str, str], set[int]] = {
    # BadRequestError, 401 authed, PermissionDenied, NotFoundError, OperationFailed. Its
    # only 503 is the reload gate's, so none is declared here.
    ("POST", "/api/run-tool"): {400, 401, 403, 404, 500},
    # 401 authed, PermissionDenied, OperationFailed, NotSupportedError, UnavailableError
    # (503 — these two doors are not reload-gated, so the dispatch seam is its only source).
    ("GET", "/api/schedules"): {401, 403, 500, 501, 503},
    ("GET", "/api/schedules/server-datetime"): {401, 403, 500, 501, 503},
    # BadRequestError, 401 authed, PermissionDenied, NotFoundError, OperationFailed,
    # NotSupportedError, UnavailableError (503) — and the reload gate's own 503 beside it.
    ("POST", "/api/schedules"): {400, 401, 403, 404, 500, 501, 503},
    # 401 authed, PermissionDenied, OperationFailed, NotSupportedError, UnavailableError
    # (503) — and the reload gate's own 503 beside it.
    ("DELETE", "/api/schedules/{schedule_name}"): {401, 403, 500, 501, 503},
}


@pytest.mark.parametrize(("method", "path"), sorted(_EXPECTED_TOOL_DISPATCH_DOOR_STATUSES))
def test_tool_dispatch_doors_declare_their_exact_status_sets(
    spec: dict, api_routes: list[RouteMetadata], method: str, path: str
) -> None:
    # Both halves are pinned by EQUALITY: the route's declared error set, and the emitted
    # operation's response codes. Beyond the declared set an emitted operation carries its
    # ``200`` success (none of the five declares a second success code —
    # ``test_declared_additional_success_set_matches_ground_truth`` pins that) and, for a
    # gated door, the reload gate's 503; so an extra or a missing code in the spec fails here.
    expected = _EXPECTED_TOOL_DISPATCH_DOOR_STATUSES[(method, path)]
    (meta,) = [m for m in api_routes if m.path == path and method in m.methods]
    assert set(meta.error_statuses) == expected
    published = {str(status) for status in expected} | {"200"}
    if meta.reload_gated:
        published.add("503")
    responses = spec["paths"][path][method.lower()]["responses"]
    assert set(responses) == published, (
        f"{method} {path} publishes {sorted(responses)}, expected the error set plus the 200 success"
    )


def test_hook_registration_documents_the_required_execution_key(spec: dict) -> None:
    # ``execution_key`` is REQUIRED on the register body, with the non-empty constraint
    # the model enforces.
    schema = spec["components"]["schemas"]["HookRegister"]
    assert "execution_key" in schema["required"]
    assert schema["properties"]["execution_key"]["type"] == "string"
    assert schema["properties"]["execution_key"]["minLength"] == 1


def test_hook_registration_omits_the_server_derived_fingerprint(spec: dict) -> None:
    # ``execution_key_fingerprint`` is server-derived at bind, never a client field, so
    # the published request contract must not list it.
    reg = spec["paths"]["/api/hooks"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert reg["$ref"].endswith("/HookRegister")
    schema = spec["components"]["schemas"]["HookRegister"]
    assert "execution_key_fingerprint" not in schema.get("properties", {})
    assert "execution_key_fingerprint" not in schema.get("required", [])
    assert "execution_key" in schema["required"]


@pytest.mark.parametrize("field", ["name", "topic", "tool", "execution_key"])
def test_hook_registration_documents_every_identifier_as_non_empty(spec: dict, field: str) -> None:
    # An empty name, topic or tool is a record no door can reach; the trigger-link mint
    # refuses the empty topic too, so both writers of a topic agree.
    schema = spec["components"]["schemas"]["HookRegister"]
    assert field in schema["required"]
    assert schema["properties"][field]["minLength"] == 1


def test_trigger_link_mint_documents_the_execution_key_and_the_door_requirement(spec: dict) -> None:
    # Same for a trigger link, plus the door's own authentication requirement, which
    # defaults to token-only (the QR-on-a-wall case).
    schema = spec["components"]["schemas"]["TriggerLinkCreate"]
    assert "execution_key" in schema["required"]
    assert schema["properties"]["execution_key"]["minLength"] == 1
    assert "require_api_key" not in schema["required"]
    assert schema["properties"]["require_api_key"]["default"] is False


@pytest.mark.parametrize("path", ["/api/hooks", "/api/hooks/trigger-links"])
def test_the_execution_key_bind_doors_document_their_refusals(
    spec: dict, api_routes: list[RouteMetadata], path: str
) -> None:
    # The bind gate answers 403 (not owned, or absent — identical, so the door is no
    # existence oracle) and 404 (unknown key, for an admin); both doors document both.
    (meta,) = [m for m in api_routes if m.path == path and "POST" in m.methods]
    assert {403, 404} <= set(meta.error_statuses)
    responses = spec["paths"][path]["post"]["responses"]
    for status in ("403", "404"):
        assert status in responses, f"POST {path} is missing the {status} response"


def test_conversation_message_door_documents_both_success_codes(spec: dict) -> None:
    # 202 when the answer is delivered out of band, 200 when a bounded wait carries it
    # inline; both documented as the ``{"data": ...}`` envelope.
    responses = spec["paths"]["/api/conversations/{route_name}/messages"]["post"]["responses"]
    assert "202" in responses
    assert "200" in responses
    for status in ("200", "202"):
        assert responses[status]["content"]["application/json"]["schema"]["required"] == ["data"]


def test_declared_additional_success_set_matches_ground_truth(api_routes: list[RouteMetadata]) -> None:
    # Ground-truth set: only the conversation send door declares a second success code.
    declared = {
        (method, meta.path): meta.additional_success_statuses
        for meta in api_routes
        if meta.additional_success_statuses
        for method in meta.methods
    }
    assert declared == {("POST", "/api/conversations/{route_name}/messages"): (200,)}


def test_conversation_message_body_requires_speaker_and_text(spec: dict) -> None:
    # Speaker and text are both required and non-empty, so an unroutable message is
    # refused at the request edge rather than stored.
    schema = spec["components"]["schemas"]["ConversationMessage"]
    assert set(schema["required"]) == {"external_user_id", "text"}
    for field in ("external_user_id", "text"):
        assert schema["properties"][field]["minLength"] == 1


def test_conversation_message_body_documents_the_optional_wait_seconds(spec: dict) -> None:
    # ``wait_seconds`` is a body field on the write door, so a client reading the spec can
    # discover the sync-wait option: optional (absent from ``required``) and non-negative.
    schema = spec["components"]["schemas"]["ConversationMessage"]
    assert "wait_seconds" not in schema["required"]
    prop = schema["properties"]["wait_seconds"]
    (int_variant,) = [v for v in prop["anyOf"] if v.get("type") == "integer"]
    assert int_variant["minimum"] == 0
    assert {"type": "null"} in prop["anyOf"]

    body = spec["paths"]["/api/conversations/{route_name}/messages"]["post"]["requestBody"]
    assert body["content"]["application/json"]["schema"]["$ref"].endswith("/ConversationMessage")


def test_conversation_route_create_requires_the_target_and_execution_key(spec: dict) -> None:
    # Target kind/name and execution key are all required: a route naming no target or no
    # identity cannot be stored.
    schema = spec["components"]["schemas"]["ConversationRouteCreate"]
    assert {"route_name", "door", "target_kind", "target_name", "execution_key"} <= set(schema["required"])
    for field in ("target_name", "execution_key"):
        assert schema["properties"][field]["minLength"] == 1


def test_conversation_route_create_omits_the_server_minted_fields(spec: dict) -> None:
    # ``callback_secret`` and ``execution_key_fingerprint`` are both server-derived, so
    # the create request contract lists neither.
    schema = spec["components"]["schemas"]["ConversationRouteCreate"]
    for field in ("callback_secret", "execution_key_fingerprint"):
        assert field not in schema.get("properties", {})
        assert field not in schema.get("required", [])
    assert "execution_key" in schema["required"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/conversations/messages/failed",
        "/api/conversations/{route_name}/threads",
    ],
)
def test_admin_listing_doors_document_the_403_refusal(spec: dict, api_routes: list[RouteMetadata], path: str) -> None:
    # The admin-only listing doors are whole-door admin gates: a non-admin caller is
    # refused 403 before any record is read, and each documents that status.
    (meta,) = [m for m in api_routes if m.path == path and "GET" in m.methods]
    assert 403 in meta.error_statuses
    assert "403" in spec["paths"][path]["get"]["responses"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/conversations/{route_name}/transcript",
        "/api/conversations/{route_name}/messages/{message_id}",
    ],
)
def test_grant_gated_read_doors_document_no_403(spec: dict, api_routes: list[RouteMetadata], path: str) -> None:
    # The grant-gated read doors never answer an authorization verdict: every refusal is
    # a uniform 404, so a guessable id cannot tell "yours, refused" from "no such record".
    (meta,) = [m for m in api_routes if m.path == path and "GET" in m.methods]
    assert 403 not in meta.error_statuses
    assert 404 in meta.error_statuses
    assert "403" not in spec["paths"][path]["get"]["responses"]


def test_the_thread_read_doors_document_their_query_parameters(spec: dict) -> None:
    # Both doors parse their query at the HTTP edge, so nothing else tells a generated
    # client what to send. Published with no parameters at all, a client calls the
    # transcript door without the REQUIRED thread_id and is answered 400 every time.
    threads = spec["paths"]["/api/conversations/{route_name}/threads"]["get"]
    assert "requestBody" not in threads
    params = {p["name"]: p for p in threads["parameters"]}
    assert params["route_name"]["in"] == "path"
    # The listing also publishes its optional ``status``/``address`` filters.
    assert {name for name, p in params.items() if p["in"] == "query"} == {"page", "pageSize", "status", "address"}
    assert params["page"]["required"] is False
    assert params["pageSize"]["required"] is False
    assert params["status"]["required"] is False
    assert params["address"]["required"] is False
    # Both bounds the door enforces: a page below 1 or past MAX_THREAD_PAGE is a 400, so a
    # generated client refuses the window before the round trip.
    assert params["page"]["schema"]["minimum"] == 1
    assert params["page"]["schema"]["maximum"] == MAX_THREAD_PAGE

    transcript = spec["paths"]["/api/conversations/{route_name}/transcript"]["get"]
    assert "requestBody" not in transcript
    read = {p["name"]: p for p in transcript["parameters"]}
    # The transcript also publishes its optional ``q`` text filter.
    assert {name for name, p in read.items() if p["in"] == "query"} == {"thread_id", "page", "pageSize", "order", "q"}
    # The one required query value: a client generated without it cannot call the door.
    assert read["thread_id"]["required"] is True
    assert read["thread_id"]["schema"]["type"] == "string"
    assert read["page"]["schema"]["minimum"] == 1
    assert read["page"]["schema"]["maximum"] == MAX_THREAD_PAGE
    assert read["order"]["required"] is False
    assert read["order"]["schema"]["enum"] == ["asc", "desc"]


def test_thread_delete_door_documents_its_required_thread_id_query(spec: dict) -> None:
    # The delete door is a WRITE method that reads thread_id from the query at the edge, so
    # it declares a query_model rather than a request_model: published with no parameters, a
    # generated client calls it with no thread to forget and is answered 400 every time. It
    # documents no body — the query_model rides ``in: query``, never a requestBody.
    delete = spec["paths"]["/api/conversations/{route_name}/thread"]["delete"]
    assert "requestBody" not in delete
    params = {p["name"]: p for p in delete["parameters"]}
    assert params["route_name"]["in"] == "path"
    assert params["thread_id"]["in"] == "query"
    assert params["thread_id"]["required"] is True
    assert params["thread_id"]["schema"]["type"] == "string"


def test_failed_mcps_door_documents_its_optional_targets_array_query(spec: dict) -> None:
    # The failed-MCP listing reads a REPEATED ``?targets=`` query at the edge; being a read
    # door it publishes its request_model as query params, so ``targets`` rides as an OPTIONAL
    # array (a repeated ``?targets=`` param under the default form serialization) and a
    # generated client can restrict the fan-out to named workers.
    get = spec["paths"]["/api/mcp-status/failed"]["get"]
    assert "requestBody" not in get
    params = {p["name"]: p for p in get["parameters"]}
    assert params["targets"]["in"] == "query"
    assert params["targets"]["required"] is False
    assert params["targets"]["schema"]["type"] == "array"
    assert params["targets"]["schema"]["items"]["type"] == "string"


# Each door here parses its query at the HTTP edge, so nothing but its declared query model
# tells a generated client what to send: the operation doors publish their ``request_model`` as
# query params, and the handler-native runs export publishes its ``query_model``. The value pins
# each door's emitted query as (required-names, optional-names, array-names), so a drift from the
# edge extractor is caught here.
_READ_DOOR_QUERIES: dict[str, tuple[set[str], set[str], set[str]]] = {
    "/api/resources/get": ({"resource_id"}, set(), set()),
    "/api/tool-runs": ({"tool_name"}, set(), set()),
    "/api/hooks": (set(), {"topic"}, set()),
    "/api/connectors/connections": (set(), {"health", "limit"}, set()),
    "/api/observability/metrics": (set(), {"from", "to", "granularity"}, set()),
    "/api/observability/runs": (
        set(),
        {
            "from",
            "to",
            "tags",
            "status",
            "minCost",
            "maxCost",
            "minTokens",
            "maxTokens",
            "minLatencyMs",
            "maxLatencyMs",
            "sort",
            "dir",
            "page",
            "pageSize",
        },
        set(),
    ),
    "/api/observability/runs/export": (
        set(),
        {
            "from",
            "to",
            "tags",
            "status",
            "minCost",
            "maxCost",
            "minTokens",
            "maxTokens",
            "minLatencyMs",
            "maxLatencyMs",
            "sort",
            "dir",
            "format",
        },
        set(),
    ),
    "/api/marketplace/search": (
        set(),
        {"q", "kind", "category", "tags", "namespace", "tier", "contract", "sort", "page", "page_size"},
        {"tags"},
    ),
    "/api/conversations/{route_name}/messages/search": ({"q"}, {"page", "pageSize"}, set()),
}


@pytest.mark.parametrize("path", sorted(_READ_DOOR_QUERIES))
def test_read_doors_publish_their_extractor_query_params(spec: dict, path: str) -> None:
    required, optional, arrays = _READ_DOOR_QUERIES[path]
    get = spec["paths"][path]["get"]
    # A read door reads its inputs from the query string, never a JSON body.
    assert "requestBody" not in get
    query = {p["name"]: p for p in get["parameters"] if p["in"] == "query"}
    assert set(query) == required | optional
    for name in required:
        # A required query value: a client generated without it cannot call the door.
        assert query[name]["required"] is True, f"{path} {name} must be required"
        assert query[name]["schema"]["type"] == "string"
    for name in optional:
        assert query[name]["required"] is False, f"{path} {name} must be optional"
    for name in arrays:
        # A repeated ``?name=`` param keeps its array schema under the default form serialization.
        assert query[name]["schema"]["type"] == "array"
        assert query[name]["schema"]["items"]["type"] == "string"


# A query param the door answers 400 on when its value is blank, with the flag saying whether
# the door refuses a WHITESPACE-ONLY value too. The spec must forbid exactly what the door
# refuses, or a generated client is free to send a value rejected every time: ``minLength``
# alone still admits ``?name=%20``, so a door refusing that publishes a ``\S`` pattern as well
# (contains at least one non-whitespace character), and one refusing only the empty value must
# not publish it.
_NON_EMPTY_QUERY_IDS = [
    ("/api/tool-runs", "get", "tool_name", False),
    ("/api/conversations/{route_name}/transcript", "get", "thread_id", True),
    ("/api/conversations/{route_name}/thread", "delete", "thread_id", True),
    ("/api/conversations/{route_name}/messages/search", "get", "q", True),
]


@pytest.mark.parametrize(("path", "method", "name", "non_blank"), _NON_EMPTY_QUERY_IDS)
def test_required_id_query_params_forbid_what_their_door_refuses(
    spec: dict, path: str, method: str, name: str, non_blank: bool
) -> None:
    (param,) = [p for p in spec["paths"][path][method]["parameters"] if p["name"] == name]
    assert param["in"] == "query"
    assert param["required"] is True
    assert param["schema"]["minLength"] == 1
    if non_blank:
        assert param["schema"]["pattern"] == r"\S", f"{method} {path} {name} admits a whitespace-only value"
    else:
        assert "pattern" not in param["schema"], f"{method} {path} {name} forbids more than its door refuses"


# A query param whose value set is CLOSED at the edge (every other value is a 400), mapped to the
# exact vocabulary the door parses. Published as a plain string these read as free text, so a
# generated client offers no choices and cannot refuse a typo before the round trip.
_CLOSED_QUERY_VALUE_SETS: dict[tuple[str, str, str], list[str]] = {
    ("/api/observability/metrics", "get", "granularity"): ["hour", "day", "week"],
    ("/api/observability/runs", "get", "status"): ["error", "success"],
    ("/api/observability/runs", "get", "sort"): ["createdAt", "cost", "latencyMs", "totalTokens"],
    ("/api/observability/runs", "get", "dir"): ["asc", "desc"],
    ("/api/observability/runs/export", "get", "format"): ["csv", "json"],
    ("/api/connectors/connections", "get", "health"): ["healthy", "reconnect_required", "refresh_failing"],
    ("/api/conversations/{route_name}/transcript", "get", "order"): ["asc", "desc"],
}


@pytest.mark.parametrize("key", sorted(_CLOSED_QUERY_VALUE_SETS))
def test_closed_value_query_params_publish_their_enum(spec: dict, key: tuple[str, str, str]) -> None:
    path, method, name = key
    (param,) = [p for p in spec["paths"][path][method]["parameters"] if p["name"] == name]
    assert param["schema"]["type"] == "string"
    assert param["schema"]["enum"] == _CLOSED_QUERY_VALUE_SETS[key]


def test_no_query_parameter_publishes_a_nullable_schema(spec: dict) -> None:
    # A query string carries no JSON ``null``: a client omits a parameter, it never sends one
    # valued null. An optional field's ``anyOf [T, null]`` + ``default: null`` would tell a
    # generated client to send a value it cannot encode, so neither survives emission.
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            for param in op.get("parameters", []):
                where = f"{method} {path} {param['name']}"
                schema = param["schema"]
                assert {"type": "null"} not in schema.get("anyOf", []), where
                assert not ("default" in schema and schema["default"] is None), where
    # Not vacuous: an optional param that pydantic renders as ``T | None`` publishes plain ``T``.
    (tags,) = [p for p in spec["paths"]["/api/observability/runs"]["get"]["parameters"] if p["name"] == "tags"]
    assert tags["required"] is False
    assert tags["schema"]["type"] == "string"


def test_query_parameters_describe_at_the_parameter_not_in_the_schema(spec: dict) -> None:
    # The Parameter Object's own ``description`` is what a generator renders; left inside the
    # schema, a field's description reaches only a schema-aware reader.
    described = 0
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            for param in op.get("parameters", []):
                assert "description" not in param["schema"], f"{method} {path} {param['name']}"
                described += "description" in param
    assert described, "no query parameter carries a description — the lift is unpinned"

    delete = spec["paths"]["/api/conversations/{route_name}/thread"]["delete"]
    (thread_id,) = [p for p in delete["parameters"] if p["name"] == "thread_id"]
    assert thread_id["description"] == "The thread to forget, as the send door returned it."


def test_a_multi_type_optional_query_param_keeps_its_real_branches() -> None:
    # Only the null branch and the null default go: a union of several REAL types keeps its
    # ``anyOf``. No shipped model is such a union, so the rule is pinned on a synthetic one.
    class _Multi(BaseModel):
        value: str | int | None = Field(default=None, description="Either shape, or omitted.")

    (param,) = _query_parameters(_Multi, {})
    assert param["required"] is False
    assert param["description"] == "Either shape, or omitted."
    assert param["schema"]["anyOf"] == [{"type": "string"}, {"type": "integer"}]
    assert "default" not in param["schema"]


def _probe_meta(
    path: str,
    *,
    method: str = "GET",
    action: RouteAction = "read",
    reads_body: bool = False,
    request_model: type[BaseModel] | None = None,
    query_model: type[BaseModel] | None = None,
) -> RouteMetadata:
    """A synthetic route for the emitter's per-operation builder — a read GET by default,
    any other method under ``method``/``action``/``reads_body``."""
    return RouteMetadata(
        path=path,
        methods=(method,),
        name="_probe",
        summary="probe",
        description="",
        tags=("probe",),
        authed=True,
        request_model=request_model,
        response_model=None,
        reload_gated=False,
        reads_body=reads_body,
        error_statuses=(),
        success_status=200,
        additional_success_statuses=(),
        success_media_types={method: ("application/json",)},
        action=action,
        query_model=query_model,
    )


def test_query_model_composes_with_a_write_body_without_conflict() -> None:
    # A write door may declare BOTH a request_model (its JSON body) and a query_model (query it
    # reads at the edge): the body rides ``requestBody`` and the query fields ride ``in: query``,
    # so the two never conflate. No shipped door carries both, so the compose rule is pinned by
    # emitting a synthetic route directly through the emitter's per-operation builder.
    class _Body(BaseModel):
        payload: str

    class _Query(BaseModel):
        token: str = Field(description="A required query token.")

    meta = _probe_meta(
        "/api/_probe/{id}", method="POST", action="write", reads_body=True, request_model=_Body, query_model=_Query
    )
    components: dict = {}
    op = _emit_operation(meta, "POST", components)
    assert op["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/_Body")
    params = {p["name"]: p for p in op["parameters"]}
    assert params["id"]["in"] == "path"
    assert params["token"]["in"] == "query"
    assert params["token"]["required"] is True
    assert params["token"]["schema"]["type"] == "string"


def test_emission_refuses_a_route_whose_query_sources_claim_one_name() -> None:
    # A read door's request_model and its query_model both ride ``in: query``, so a shared field
    # name assembles a duplicate ``(name, in)`` pair — an invalid OpenAPI document. Emission
    # fails LOUDLY naming the route and the parameter rather than shipping it. No shipped door
    # collides, so the guard is pinned on a synthetic route through the per-operation builder.
    class _Read(BaseModel):
        token: str = Field(description="Read from the query at the edge.")

    class _Query(BaseModel):
        token: str = Field(description="The same query key, declared twice.")

    meta = _probe_meta("/api/_probe", request_model=_Read, query_model=_Query)
    with pytest.raises(ValueError, match=r"GET /api/_probe declares parameter 'token' in query twice"):
        _emit_operation(meta, "GET", {})


def test_emission_refuses_a_route_whose_path_repeats_a_parameter() -> None:
    # Path parameters take part in the same uniqueness scan: a path naming one twice emits two
    # identical ``in: path`` entries, which is equally invalid and equally loud.
    meta = _probe_meta("/api/_probe/{id}/nested/{id}")
    with pytest.raises(ValueError, match=r"declares parameter 'id' in path twice"):
        _emit_operation(meta, "GET", {})


def test_runs_export_documents_both_csv_and_json_download(spec: dict) -> None:
    # The runs export serves either a CSV body or a JSON download from one GET, so
    # its 200 lists both content types rather than being pinned to CSV alone.
    content = spec["paths"]["/api/observability/runs/export"]["get"]["responses"]["200"]["content"]
    assert "text/csv" in content
    assert "application/octet-stream" in content


def test_register_model_rejects_a_reserved_envelope_name() -> None:
    class Error(BaseModel):
        detail: str

    with pytest.raises(ValueError, match="reserved"):
        _register_model(Error, {})


def test_register_model_rejects_a_conflicting_same_name_schema() -> None:
    components: dict = {}
    _assign_component(components, "Widget", {"type": "object", "properties": {"a": {"type": "integer"}}})
    with pytest.raises(ValueError, match="collision"):
        _assign_component(components, "Widget", {"type": "object", "properties": {"b": {"type": "string"}}})


def test_register_model_allows_idempotent_reregistration() -> None:
    class Gadget(BaseModel):
        a: int

    components: dict = {}
    assert _register_model(Gadget, components) == "Gadget"
    # The same model reached from a second route registers identically — no raise.
    assert _register_model(Gadget, components) == "Gadget"


def test_error_schema_documents_the_optional_machine_readable_code(spec: dict) -> None:
    # The shared Error component documents both fields the adapter emits: the required
    # human-readable ``error`` and the OPTIONAL machine-readable ``code`` a refusal opts
    # into (the 501 not-configured family). ``code`` is a string and absent from
    # ``required``, so an error carrying only ``error`` still validates — additive and
    # non-breaking.
    schema = spec["components"]["schemas"]["Error"]
    assert schema["required"] == ["error"]
    assert schema["properties"]["error"]["type"] == "string"
    assert schema["properties"]["code"]["type"] == "string"
    assert "code" not in schema["required"]

    # A bare error and a coded error both validate against the shared component.
    validator = Draft202012Validator({**schema, "components": spec["components"]})
    validator.validate({"error": "boom"})
    validator.validate({"error": "not configured", "code": "marketplace-not-configured"})


def test_reloading_error_schema_matches_the_gate_body(spec: dict) -> None:
    schema = spec["components"]["schemas"]["ReloadingError"]
    assert schema["properties"]["error"]["const"] == REJECT_MESSAGE
    assert schema["properties"]["reloading"]["const"] is True


def test_authed_routes_require_the_api_key_security(spec: dict, api_routes: list[RouteMetadata]) -> None:
    for meta in api_routes:
        for method in meta.methods:
            op = _operation(spec, meta, method)
            if meta.authed:
                assert op["security"] == [{"ApiKeyAuth": []}], f"{meta.path} missing api-key security"
                assert "401" in op["responses"]
            else:
                assert "security" not in op


def test_body_routes_declare_a_request_body(spec: dict, api_routes: list[RouteMetadata]) -> None:
    # A requestBody is documented ONLY for a body-reading (write) method. A read method
    # (GET/HEAD) parses its typed parameters from the query string, so even when its
    # operation carries a ``request_model`` it documents no body — a GET request body
    # would misdocument the endpoint.
    for meta in api_routes:
        if meta.request_model is None:
            continue
        for method in meta.methods:
            op = _operation(spec, meta, method)
            if method_to_action(method) == "write":
                ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/" + meta.request_model.__name__)
                assert meta.request_model.__name__ in spec["components"]["schemas"]
            else:
                assert "requestBody" not in op, f"{method} {meta.path} must not document a request body"


def test_resources_get_read_route_documents_query_params_not_a_body(
    spec: dict, api_routes: list[RouteMetadata]
) -> None:
    # The read-classed GET fetch door shares the operation (and its ``request_model``)
    # with the write-classed POST render door on the same path. The GET must self-describe
    # its input as ``in: query`` parameters (NO requestBody), so a generated client knows
    # ``resource_id`` is a REQUIRED query param; the POST keeps its ``ResourceGet`` body.
    (meta,) = [m for m in api_routes if m.path == "/api/resources/get" and "GET" in m.methods]
    assert meta.action == "read"
    assert meta.reads_body is False

    get_op = spec["paths"]["/api/resources/get"]["get"]
    assert "requestBody" not in get_op

    params = {p["name"]: p for p in get_op.get("parameters", [])}
    # ``resource_id`` is the door's sole query input, documented as a REQUIRED string param.
    assert set(params) == {"resource_id"}
    assert params["resource_id"]["in"] == "query"
    assert params["resource_id"]["required"] is True
    assert params["resource_id"]["schema"]["type"] == "string"
    # ``template_kwargs`` is a body input the render POST takes, never a GET query value (a query
    # string cannot carry the nested object it is), so it is absent from the GET and rides the
    # POST's ``ResourceGet`` body alone — a generated GET client is never told to send a value
    # every request would reject.
    assert "template_kwargs" not in params
    post_op = spec["paths"]["/api/resources/get"]["post"]
    assert post_op["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ResourceGet")
    body = spec["components"]["schemas"]["ResourceGet"]
    assert "template_kwargs" in body["properties"]


def test_spec_validates_against_openapi_31(spec: dict) -> None:
    validate(spec)
    assert spec["openapi"] == "3.1.0"


# -- Offline emission --------------------------------------------------------


def test_emission_touches_no_db_or_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building the spec must not open a client — patch the pooled-client seam to
    raise, then prove emission still succeeds and validates."""
    import tai42_kit.clients as clients

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("spec emission must not open a database/Redis client")

    monkeypatch.setattr(clients, "client_ctx", _forbidden)
    spec = build_openapi_spec()
    validate(spec)


def test_emission_runs_in_a_bare_process_without_db_or_redis_env() -> None:
    """A fresh interpreter with all DB/Redis/manifest env stripped emits a valid
    spec — the docs pipeline's usage, with no environment booted."""
    import os

    stripped = {
        k: v
        for k, v in os.environ.items()
        if not any(token in k.upper() for token in ("REDIS", "POSTGRES", "DATABASE", "TAI_"))
    }
    code = (
        "import json;"
        "from tai42_skeleton.cli.openapi import build_openapi_spec;"
        "from openapi_spec_validator import validate;"
        "s = build_openapi_spec();"
        "validate(s);"
        "print('OK', len(s['paths']))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=stripped,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK ")


# -- ``tai openapi`` command -------------------------------------------------


def test_openapi_command_prints_valid_spec_to_stdout() -> None:
    result = CliRunner().invoke(app_module.app, ["openapi"])
    assert result.exit_code == 0, result.output
    validate(json.loads(result.output))


def test_openapi_command_writes_to_out_path(tmp_path) -> None:
    target = tmp_path / "openapi.json"
    result = CliRunner().invoke(app_module.app, ["openapi", "--out", str(target)])
    assert result.exit_code == 0, result.output
    validate(json.loads(target.read_text()))


def test_openapi_check_succeeds_on_a_valid_spec() -> None:
    result = CliRunner().invoke(app_module.app, ["openapi", "--check"])
    assert result.exit_code == 0, result.output
