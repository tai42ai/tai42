"""OpenAPI emission — models and schemas: register_model, no-body reasons,
enveloped-false raw bodies, RootModel components, the shared error/security schemas,
and the ``tai openapi`` command."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from pydantic import BaseModel, RootModel
from tai42_cli import app as app_module

from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.app.route_registry import RouteMetadata, method_to_action
from tai42_skeleton.cli.openapi import (
    _assign_component,
    _register_model,
    _success_response,
    build_openapi_spec,
)
from tai42_skeleton.cli.openapi import _operation as _emit_operation

from .conftest import _operation


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


# -- no_body_reason: the reasoned no-body declaration surfaces in the spec ------


def _typed_meta(response_model, *, no_body_reason=None, enveloped=True) -> RouteMetadata:
    """A synthetic core JSON route carrying ``response_model`` (or a reasoned no-body),
    for the emitter's per-operation/success builders — no product surface loaded."""
    return RouteMetadata(
        path="/api/_probe",
        methods=("POST",),
        name="_probe",
        summary="probe",
        description="",
        tags=("probe",),
        authed=True,
        request_model=None,
        response_model=response_model,
        reload_gated=False,
        reads_body=False,
        error_statuses=(),
        success_status=200,
        additional_success_statuses=(),
        success_media_types={"POST": ("application/json",)},
        action="write",
        no_body_reason=no_body_reason,
        enveloped=enveloped,
    )


def test_no_body_reason_becomes_the_success_description_and_extension() -> None:
    # A route declared with no typed body carries its no_body_reason as the 200's
    # description AND an x-no-body extension, so the absence of a {"data": <model>}
    # schema is a described, declared exception rather than a silent empty data.
    reason = "serves a raw streaming body, not the {data} envelope"
    meta = _typed_meta(None, no_body_reason=reason)
    response = _success_response(meta, "POST", {})
    assert response["description"] == reason
    assert response["x-no-body"] == reason
    # The data schema stays the empty None-branch object (no reshaping of the wire).
    assert response["content"]["application/json"]["schema"]["properties"]["data"] == {}


def test_typed_route_success_carries_no_no_body_extension() -> None:
    # A typed route documents its {"data": $ref} schema and never an x-no-body marker.
    class _Body(BaseModel):
        value: int

    meta = _typed_meta(_Body)
    components: dict = {}
    response = _success_response(meta, "POST", components)
    assert "x-no-body" not in response
    assert response["description"] == "Success."
    assert response["content"]["application/json"]["schema"]["properties"]["data"] == {
        "$ref": "#/components/schemas/_Body"
    }


# -- enveloped=False: the RAW top-level body renders as the model's $ref directly --


def test_unwrapped_model_renders_as_a_top_level_body_schema() -> None:
    # A route declared enveloped=False publishes its model's schema DIRECTLY as the
    # 200 application/json body — a $ref with NO {"data": ...} wrapper and NO x-no-body
    # marker, so a raw non-enveloped body carries a real schema rather than a reasoned
    # no-body exception.
    class _Raw(BaseModel):
        status: str

    meta = _typed_meta(_Raw, enveloped=False)
    components: dict = {}
    response = _success_response(meta, "POST", components)
    schema = response["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/_Raw"}
    assert "properties" not in schema  # no data envelope
    assert "x-no-body" not in response
    assert response["description"] == "Success."
    assert "_Raw" in components


def test_the_four_raw_json_routes_emit_their_real_top_level_schema() -> None:
    # Each of the four RAW-JSON routes now declares a response_model + enveloped=False,
    # so its 200 application/json body is the model's schema directly (no {"data": ...}
    # wrapper, no x-no-body). The metadata is read from the live registry, not a
    # synthetic probe, so this pins the actual registrations.
    from tai42_skeleton.app.route_registry import load_all_routes

    expected = {
        ("/ready", "GET"): "ReadinessStatus",
        ("/universal_webhook/{topic}", "POST"): "WebhookIngressResult",
        ("/trigger/{token}", "POST"): "TriggerResult",
        ("/api/backup/export", "POST"): "BackupExportDocument",
    }
    by_path = {meta.path: meta for meta in load_all_routes()}
    for (path, method), model_name in expected.items():
        meta = by_path.get(path)
        assert meta is not None, f"{path} not registered"
        assert meta.enveloped is False, f"{path} is not declared enveloped=False"
        assert meta.no_body_reason is None, f"{path} still carries a no_body_reason"
        components: dict = {}
        op = _emit_operation(meta, method, components)
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model_name}"}, path
        assert "x-no-body" not in op["responses"]["200"], path
        assert model_name in components, path


# -- RootModel bodies render as a registered, resolvable component --------------


def test_opaque_json_root_model_renders_a_resolvable_ref() -> None:
    from tai42_contract.app.responses import OpaqueJson

    meta = _typed_meta(OpaqueJson)
    components: dict = {}
    op = _emit_operation(meta, "POST", components)
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["data"]["$ref"]
    assert ref == "#/components/schemas/OpaqueJson"
    # The named subclass registers under its own stable name and its schema matches
    # the model's own JSON schema (a bare alias would register the ugly RootModel name).
    assert "OpaqueJson" in components
    assert OpaqueJson.__name__ == "OpaqueJson"


def test_named_root_model_list_subclass_renders_and_resolves() -> None:
    class Point(BaseModel):
        x: int

    class PointList(RootModel[list[Point]]):
        pass

    meta = _typed_meta(PointList)
    components: dict = {}
    op = _emit_operation(meta, "POST", components)
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["data"]["$ref"]
    assert ref == "#/components/schemas/PointList"
    # The component resolves and its members' $defs are registered (no dangling ref).
    assert components["PointList"] == {
        "items": {"$ref": "#/components/schemas/Point"},
        "type": "array",
        "title": "PointList",
    }
    assert "Point" in components


# -- The three already-typed core response models still render their $ref -------


def test_already_typed_core_models_render_their_ref() -> None:
    # The models the three already-typed core ops declare (unchanged by this seam)
    # each register and emit a resolvable {"data": $ref}, so a typed route never hits
    # the None-branch guard.
    from tai42_skeleton.access_control.projection import ProjectionResult
    from tai42_skeleton.app.bus import FleetResult
    from tai42_skeleton.operations.backend import WorkerListing

    for model in (ProjectionResult, WorkerListing, FleetResult):
        components: dict = {}
        op = _emit_operation(_typed_meta(model), "POST", components)
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["data"]["$ref"]
        assert ref == f"#/components/schemas/{model.__name__}"
        assert model.__name__ in components


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
    # ``kwargs`` is a body input the render POST takes, never a GET query value (a query
    # string cannot carry the nested object it is), so it is absent from the GET and rides the
    # POST's ``ResourceGet`` body alone — a generated GET client is never told to send a value
    # every request would reject.
    assert "kwargs" not in params
    post_op = spec["paths"]["/api/resources/get"]["post"]
    assert post_op["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ResourceGet")
    body = spec["components"]["schemas"]["ResourceGet"]
    assert "kwargs" in body["properties"]


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
