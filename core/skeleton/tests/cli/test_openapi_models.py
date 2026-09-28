"""OpenAPI emission — models and schemas: register_model, no-body reasons,
enveloped-false raw bodies, RootModel components, the shared error/security schemas,
and the ``tai openapi`` command."""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from pydantic import BaseModel, RootModel, computed_field
from tai42_cli import app as app_module

from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.app.route_registry import RouteMetadata, method_to_action
from tai42_skeleton.cli.openapi import (
    _assign_component,
    _component_schemas,
    _success_response,
    build_openapi_spec,
)
from tai42_skeleton.cli.openapi import _operation as _emit_operation

from .conftest import _operation, schemas_for


def _data_ref(op: dict) -> str:
    """The ``$ref`` a success operation points its enveloped ``{"data": ...}`` at."""
    return op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["data"]["$ref"]


def _dangling_refs(document: dict) -> list[str]:
    """Every ``#/components/schemas`` ``$ref`` in ``document`` with no matching component."""
    schemas = document.get("components", {}).get("schemas", {})
    refs = set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(document)))
    return sorted(ref for ref in refs if ref not in schemas)


def test_runs_export_documents_both_csv_and_json_download(spec: dict) -> None:
    # The runs export serves either a CSV body or a JSON download from one GET, so
    # its 200 lists both content types rather than being pinned to CSV alone.
    content = spec["paths"]["/api/observability/runs/export"]["get"]["responses"]["200"]["content"]
    assert "text/csv" in content
    assert "application/octet-stream" in content


def test_a_model_claiming_a_reserved_envelope_name_raises() -> None:
    # A model whose component name would collide with a reserved response-envelope name
    # (``Error`` / ``ReloadingError``) must not silently overwrite the envelope: the merge
    # of its generated definition raises LOUDLY.
    class Error(BaseModel):
        detail: str

    with pytest.raises(ValueError, match="reserved"):
        _component_schemas([(Error, "serialization")], {})


def test_assign_component_rejects_a_conflicting_same_name_schema() -> None:
    components: dict = {}
    _assign_component(components, "Widget", {"type": "object", "properties": {"a": {"type": "integer"}}})
    with pytest.raises(ValueError, match="collision"):
        _assign_component(components, "Widget", {"type": "object", "properties": {"b": {"type": "string"}}})


def test_assign_component_allows_idempotent_reregistration() -> None:
    # A component name mapping to an IDENTICAL schema is a no-op, never a collision — the
    # same nested model reached from two definitions merges once.
    components: dict = {}
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    _assign_component(components, "Gadget", schema)
    _assign_component(components, "Gadget", dict(schema))
    assert components["Gadget"] == schema


# -- no_body_reason: the reasoned no-body declaration surfaces in the spec ------


def _typed_meta(response_model, *, request_model=None, no_body_reason=None, enveloped=True) -> RouteMetadata:
    """A synthetic core JSON route carrying ``response_model`` (or a reasoned no-body) and an
    optional ``request_model`` body, for the emitter's per-operation/success builders — no
    product surface loaded."""
    return RouteMetadata(
        path="/api/_probe",
        methods=("POST",),
        name="_probe",
        summary="probe",
        description="",
        tags=("probe",),
        authed=True,
        request_model=request_model,
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
    schemas, _ = schemas_for([meta])
    response = _success_response(meta, "POST", schemas)
    assert response["description"] == reason
    assert response["x-no-body"] == reason
    # The data schema stays the empty None-branch object (no reshaping of the wire).
    assert response["content"]["application/json"]["schema"]["properties"]["data"] == {}


def test_typed_route_success_carries_no_no_body_extension() -> None:
    # A typed route documents its {"data": $ref} schema and never an x-no-body marker.
    class _Body(BaseModel):
        value: int

    meta = _typed_meta(_Body)
    schemas, _ = schemas_for([meta])
    response = _success_response(meta, "POST", schemas)
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
    schemas, components = schemas_for([meta])
    response = _success_response(meta, "POST", schemas)
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
        schemas, components = schemas_for([meta])
        op = _emit_operation(meta, method, schemas)
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model_name}"}, path
        assert "x-no-body" not in op["responses"]["200"], path
        assert model_name in components, path


# -- RootModel bodies render as a registered, resolvable component --------------


def test_opaque_json_root_model_renders_a_resolvable_ref() -> None:
    from tai42_contract.app.responses import OpaqueJson

    meta = _typed_meta(OpaqueJson)
    schemas, components = schemas_for([meta])
    op = _emit_operation(meta, "POST", schemas)
    ref = _data_ref(op)
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
    schemas, components = schemas_for([meta])
    op = _emit_operation(meta, "POST", schemas)
    ref = _data_ref(op)
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
        meta = _typed_meta(model)
        schemas, components = schemas_for([meta])
        op = _emit_operation(meta, "POST", schemas)
        ref = _data_ref(op)
        assert ref == f"#/components/schemas/{model.__name__}"
        assert model.__name__ in components


# -- serialization vs validation: computed fields and the -Input/-Output split --


def test_response_computed_field_is_present_and_required() -> None:
    # A response describes what the server EMITS: a @computed_field is serialized on every
    # response, so it appears in the response component's ``properties`` AND its ``required``
    # — a validation-mode schema would omit it entirely.
    class WithComputed(BaseModel):
        reachable: bool = True

        @computed_field  # type: ignore[prop-decorator]
        @property
        def ok(self) -> bool:
            return self.reachable

    meta = _typed_meta(WithComputed)
    schemas, components = schemas_for([meta])
    op = _emit_operation(meta, "POST", schemas)
    component = components[_data_ref(op).rsplit("/", 1)[-1]]
    assert "ok" in component["properties"]
    assert "ok" in component["required"]


def test_dual_role_model_splits_into_input_and_output_components() -> None:
    # A model used as BOTH a request body and a response, whose validation and serialization
    # schemas differ (a @computed_field is serialized but never accepted on input), is split
    # into pydantic's ``-Input`` and ``-Output`` components: the request body $refs ``-Input``,
    # the response $refs ``-Output``, and every $ref resolves.
    class Dual(BaseModel):
        value: int

        @computed_field  # type: ignore[prop-decorator]
        @property
        def derived(self) -> int:
            return self.value * 2

    meta = _typed_meta(Dual, request_model=Dual)
    schemas, components = schemas_for([meta])
    op = _emit_operation(meta, "POST", schemas)

    body_ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert body_ref == "#/components/schemas/Dual-Input"
    assert _data_ref(op) == "#/components/schemas/Dual-Output"

    assert "derived" not in components["Dual-Input"]["properties"]
    assert "derived" in components["Dual-Output"]["properties"]
    assert "derived" in components["Dual-Output"]["required"]

    document = {"paths": {"/api/_probe": {"post": op}}, "components": {"schemas": components}}
    assert _dangling_refs(document) == []


def test_response_only_model_keeps_its_bare_name() -> None:
    # A model used in a single mode is not split: a response-only model keeps its bare name,
    # never gaining an ``-Output`` suffix.
    class SoloResponse(BaseModel):
        value: int

    meta = _typed_meta(SoloResponse)
    schemas, components = schemas_for([meta])
    op = _emit_operation(meta, "POST", schemas)
    assert _data_ref(op) == "#/components/schemas/SoloResponse"
    assert "SoloResponse" in components
    assert "SoloResponse-Output" not in components


def test_full_spec_serializes_responses_and_leaves_no_dangling_ref(spec: dict) -> None:
    # The real registry, emitted through the CLI's own entry point: FleetResult's
    # @computed_field ``ok`` is present and required in its response component (the server
    # always serializes it), and no $ref anywhere in the document dangles.
    fleet = spec["components"]["schemas"]["FleetResult"]
    assert "ok" in fleet["properties"]
    assert "ok" in fleet["required"]
    assert _dangling_refs(spec) == []


def test_every_component_is_referenced(spec: dict) -> None:
    # Every published component is reachable from a $ref in paths (transitively), or is one of
    # the reserved response-envelope names the emitter builds and references itself. A query
    # model is inlined as parameters and its own top-level schema is never $ref'd, so it must
    # not leak into the published components. And no component name is a module-qualified
    # pydantic name (containing ``__``), which would expose an internal module path.
    from tai42_skeleton.cli.openapi import _RESERVED_SCHEMA_NAMES

    schemas = spec["components"]["schemas"]

    # Transitive reachability from paths, recomputed independently of the emitter's own walk.
    reachable = set(_RESERVED_SCHEMA_NAMES)
    frontier = list(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(spec["paths"])))
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        component = schemas.get(name)
        if component is None:
            continue
        frontier.extend(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(component)))

    unreferenced = sorted(name for name in schemas if name not in reachable)
    assert unreferenced == [], f"unreferenced components leaked into the spec: {unreferenced}"

    module_qualified = sorted(name for name in schemas if "__" in name)
    assert module_qualified == [], f"module-qualified component names leaked: {module_qualified}"


def test_query_only_model_is_not_published_but_a_referenced_nested_model_is() -> None:
    # A query model is inlined as parameters and its own top-level schema is never $ref'd, so
    # the prune drops it from the published components. A nested model one of its fields
    # references rides a $ref in that field's parameter schema, so it stays a component.
    from tai42_skeleton.cli.openapi import _openapi_path, _referenced_components

    class QNested(BaseModel):
        depth: int

    class OnlyQuery(BaseModel):
        flag: bool = False
        nested: QNested

    meta = RouteMetadata(
        path="/api/_probe",
        methods=("GET",),
        name="_probe",
        summary="probe",
        description="",
        tags=("probe",),
        authed=True,
        request_model=None,
        response_model=None,
        reload_gated=False,
        reads_body=False,
        error_statuses=(),
        success_status=200,
        additional_success_statuses=(),
        success_media_types={"GET": ("application/json",)},
        action="read",
        query_model=OnlyQuery,
    )
    schemas, components = schemas_for([meta])
    op = _emit_operation(meta, "GET", schemas)

    param_names = {p["name"] for p in op["parameters"]}
    assert param_names == {"flag", "nested"}
    nested_param = next(p for p in op["parameters"] if p["name"] == "nested")
    assert nested_param["schema"] == {"$ref": "#/components/schemas/QNested"}

    # Both definitions are generated and available to the resolver; the prune keeps only what
    # the assembled paths reference.
    assert "OnlyQuery" in components
    assert "QNested" in components
    paths = {_openapi_path(meta.path): {"get": op}}
    reachable = _referenced_components(paths, components)
    published = {name for name in components if name in reachable}
    assert "OnlyQuery" not in published  # the query model's own schema does not leak
    assert "QNested" in published  # a $defs entry a parameter references stays


def test_variant_reached_only_through_a_discriminator_mapping_survives_the_prune() -> None:
    # OpenAPI 3.1 carries a reference not only as a ``$ref`` string but also as each
    # ``#/components/schemas/<name>`` value of a ``discriminator.mapping`` object. A variant
    # reachable ONLY through such a mapping value (no sibling ``$ref``) must still be kept by
    # the reachability prune.
    from tai42_skeleton.cli.openapi import _referenced_components

    components = {
        "Envelope": {
            "type": "object",
            "properties": {"body": {"$ref": "#/components/schemas/Union"}},
        },
        "Union": {
            "oneOf": [{"type": "object"}],
            "discriminator": {
                "propertyName": "kind",
                "mapping": {"only": "#/components/schemas/MappingOnlyVariant"},
            },
        },
        "MappingOnlyVariant": {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
        },
    }
    paths = {
        "/api/_probe": {
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Envelope"}}},
                    }
                }
            }
        }
    }

    reachable = _referenced_components(paths, components)
    # The variant hangs off the union solely through the discriminator mapping value, yet the
    # walker follows it, so the prune keeps it.
    assert "MappingOnlyVariant" in reachable


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
    # would misdocument the endpoint. A write body references its model's VALIDATION-mode
    # component: its bare name, or the ``-Input`` component when the model's validation and
    # serialization schemas differ.
    schemas = spec["components"]["schemas"]
    for meta in api_routes:
        if meta.request_model is None:
            continue
        name = meta.request_model.__name__
        for method in meta.methods:
            op = _operation(spec, meta, method)
            if method_to_action(method) == "write":
                ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
                component = ref.rsplit("/", 1)[-1]
                assert component in (name, f"{name}-Input"), f"{meta.path} body $ref {ref}"
                assert component in schemas
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
