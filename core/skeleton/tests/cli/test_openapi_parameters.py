"""OpenAPI emission — parameters: query-parameter derivation, path/query emission,
and the uniqueness refusals."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from tai42_skeleton.app.route_registry import RouteAction, RouteMetadata
from tai42_skeleton.cli.openapi import _operation as _emit_operation
from tai42_skeleton.cli.openapi import (
    _query_parameters,
)
from tai42_skeleton.operations.conversations import MAX_THREAD_PAGE


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
            "user",
            "session",
            "version",
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
            "user",
            "session",
            "version",
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
