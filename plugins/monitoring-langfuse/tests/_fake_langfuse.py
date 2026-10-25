"""An in-memory Langfuse server behind an ``httpx.MockTransport``, serving the two REST routes the reader calls.

It answers ``GET /api/public/v2/observations`` and ``GET /api/public/v2/metrics`` the way the server does: the
``filter`` JSON selects rows, only the requested field groups come back, rows are ordered by ``startTime`` desc and
paged by ``limit`` and ``cursor``, and a metadata value is cut to 200 characters unless ``expandMetadata`` is set.
Every request is recorded with its query parameters; queued failures are answered before any normal answer.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

OBSERVATIONS_PATH = "/api/public/v2/observations"
METRICS_PATH = "/api/public/v2/metrics"

# Each field group and the camelCase row keys it carries.
FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("id", "traceId", "startTime", "endTime", "projectId", "parentObservationId", "type"),
    "basic": (
        "name",
        "level",
        "statusMessage",
        "version",
        "environment",
        "userId",
        "sessionId",
        "isRootObservation",
    ),
    "time": ("completionStartTime", "createdAt", "updatedAt"),
    "io": ("input", "output"),
    "metadata": ("metadata",),
    "model": ("model",),
    "usage": ("usageDetails", "costDetails", "totalCost"),
    "prompt": ("promptName",),
    "metrics": ("latency",),
    "trace_context": ("tags", "traceName", "release"),
}
_OBSERVATIONS_PARAMS = frozenset({"fields", "filter", "limit", "cursor", "expandMetadata"})
_METADATA_CUT = 200
_GRANULARITY = {"hour": "%Y-%m-%dT%H:00:00Z", "day": "%Y-%m-%dT00:00:00Z"}


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Row:
    """One stored observation, in the server's own terms."""

    id: str
    trace_id: str
    start: datetime
    end: datetime | None = None
    parent_id: str | None = None
    type: str = "SPAN"
    name: str | None = None
    level: str = "DEFAULT"
    status_message: str | None = None
    version: str | None = None
    environment: str = "tai"
    user_id: str | None = None
    session_id: str | None = None
    input: str | None = None
    output: str | None = None
    # Flat keys -> the stored string value, as the server keeps span attributes.
    metadata: dict[str, str] = field(default_factory=dict[str, str])
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict[str, int])
    total_cost: float | None = None
    tags: list[str] = field(default_factory=list[str])
    trace_name: str | None = None

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def latency(self) -> float | None:
        return None if self.end is None else (self.end - self.start).total_seconds()

    @property
    def total_tokens(self) -> int:
        if "total" in self.usage:
            return self.usage["total"]
        return self.usage.get("input", 0) + self.usage.get("output", 0)

    def column(self, name: str) -> Any:
        return {
            "id": self.id,
            "traceId": self.trace_id,
            "type": self.type,
            "name": self.name,
            "level": self.level,
            "version": self.version,
            "environment": self.environment,
            "userId": self.user_id,
            "sessionId": self.session_id,
            "isRootObservation": self.is_root,
            "startTime": self.start,
            "latency": self.latency,
            "model": self.model,
            "providedModelName": self.model,
            "totalCost": self.total_cost,
            "totalTokens": self.total_tokens,
            "traceName": self.trace_name,
            "traceTags": self.tags,
            "tags": self.tags,
        }[name]

    def matches(self, clause: dict[str, Any]) -> bool:
        operator, value = clause["operator"], clause["value"]
        if clause["column"] == "metadata":
            assert clause["type"] == "stringObject"
            return self.metadata.get(clause["key"]) == value
        actual = self.column(clause["column"])
        if operator == "any of":
            assert value, "the server refuses an empty any-of list"
            if isinstance(actual, list):
                return bool(set(actual) & set(value))
            return actual in value
        if operator == "all of":
            return set(value) <= set(actual)
        if clause["type"] == "datetime":
            value = _parse_time(value)
        if operator == "=":
            return actual == value
        if actual is None:
            return False
        return {">=": actual >= value, "<=": actual <= value, ">": actual > value, "<": actual < value}[operator]

    def render(self, groups: set[str], expanded: bool) -> dict[str, Any]:
        full: dict[str, Any] = {
            "id": self.id,
            "traceId": self.trace_id,
            "startTime": iso(self.start),
            "endTime": iso(self.end) if self.end else None,
            "projectId": "project",
            "parentObservationId": self.parent_id,
            "type": self.type,
            "name": self.name,
            "level": self.level,
            "statusMessage": self.status_message,
            "version": self.version,
            "environment": self.environment,
            "userId": self.user_id,
            "sessionId": self.session_id,
            "isRootObservation": self.is_root,
            "input": self.input,
            "output": self.output,
            "metadata": {k: _metadata_value(v, expanded) for k, v in self.metadata.items()},
            "model": self.model,
            "usageDetails": dict(self.usage),
            "costDetails": {} if self.total_cost is None else {"total": self.total_cost},
            "totalCost": self.total_cost,
            "latency": self.latency,
            "tags": list(self.tags),
            "traceName": self.trace_name,
            "release": "",
        }
        keys = {key for group in groups for key in FIELD_GROUPS[group]}
        return {k: v for k, v in full.items() if k in keys}


def _metadata_value(value: str, expanded: bool) -> Any:
    if not expanded and len(value) > _METADATA_CUT:
        return value[:_METADATA_CUT]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


class FakeLangfuse:
    """The fake server: its stored rows, its request log, and the transport handler a test client is built on."""

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.calls: list[tuple[str, dict[str, str]]] = []
        # Answers given, in order, before any normal answer: an HTTP response or an exception the transport raises.
        self.failures: list[httpx.Response | Exception] = []
        self.out_of_order = False
        # Rows the server adds to every ``traceId any of`` answer, whatever trace they belong to.
        self.stray: list[Row] = []
        self._by_trace: dict[str, list[Row]] = defaultdict(list)
        self._selected: dict[str, list[Row]] = {}

    def add(self, **kw: Any) -> Row:
        row = Row(**kw)
        self.rows.append(row)
        self._by_trace[row.trace_id].append(row)
        self._selected.clear()
        return row

    def run(
        self,
        trace_id: str,
        start: datetime,
        *,
        seconds: float = 1.0,
        children: int = 0,
        child_cost: float | None = None,
        child_tokens: int | None = None,
        child_level: str = "DEFAULT",
        child_model: str | None = None,
        **root: Any,
    ) -> Row:
        """A run: a root span and ``children`` generations under it, each stamped with the root's trace columns."""
        stamped = {k: root[k] for k in ("environment", "tags", "trace_name", "user_id", "session_id") if k in root}
        made = self.add(
            id=f"{trace_id}-root", trace_id=trace_id, start=start, end=start + timedelta(seconds=seconds), **root
        )
        for n in range(children):
            self.add(
                id=f"{trace_id}-c{n}",
                trace_id=trace_id,
                parent_id=made.id,
                type="GENERATION",
                start=start,
                end=start + timedelta(seconds=seconds / 2),
                total_cost=child_cost,
                usage={} if child_tokens is None else {"total": child_tokens},
                level=child_level,
                model=child_model,
                **stamped,
            )
        return made

    def fail(self, *answers: httpx.Response | Exception) -> None:
        """Queue ``answers`` for the next requests."""
        self.failures.extend(answers)

    def calls_of(self, name: str) -> list[dict[str, str]]:
        return [params for called, params in self.calls if called == name]

    def filters_sent(self) -> list[list[dict[str, Any]]]:
        return [json.loads(params["filter"]) for params in self.calls_of("observations")]

    def handle(self, request: httpx.Request) -> httpx.Response:
        """The ``httpx.MockTransport`` handler."""
        assert request.method == "GET", request.method
        params = dict(request.url.params)
        route = {OBSERVATIONS_PATH: "observations", METRICS_PATH: "metrics"}[request.url.path]
        self.calls.append((route, params))
        if self.failures:
            answer = self.failures.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        body = self._observations(params) if route == "observations" else self._metrics(params)
        return httpx.Response(200, content=json.dumps(body).encode())

    def _candidates(self, clauses: list[dict[str, Any]]) -> list[Row]:
        for clause in clauses:
            if clause["column"] == "traceId":
                ids = [clause["value"]] if clause["operator"] == "=" else clause["value"]
                return [row for trace_id in ids for row in self._by_trace.get(trace_id, [])]
        return self.rows

    def _select(self, filter_json: str) -> list[Row]:
        if filter_json not in self._selected:
            clauses = json.loads(filter_json)
            rows = [r for r in self._candidates(clauses) if all(r.matches(c) for c in clauses)]
            if any(c["column"] == "traceId" and c["operator"] == "any of" for c in clauses):
                rows += self.stray
            rows.sort(key=lambda r: (r.start, r.trace_id, r.id), reverse=True)
            self._selected[filter_json] = rows
        return self._selected[filter_json]

    def _observations(self, params: dict[str, str]) -> dict[str, Any]:
        extra = set(params) - _OBSERVATIONS_PARAMS
        assert not extra, f"native params sent beside the filter JSON: {sorted(extra)}"
        limit = int(params["limit"])
        assert 1 <= limit <= 1000
        groups = set(params["fields"].split(","))
        assert groups <= set(FIELD_GROUPS), groups
        rows = self._select(params["filter"])
        offset = int(params.get("cursor", "0"))
        page = rows[offset : offset + limit]
        data = [row.render(groups, bool(params.get("expandMetadata"))) for row in page]
        if self.out_of_order and len(data) > 1:
            data.reverse()
        meta = {"cursor": str(offset + limit)} if offset + limit < len(rows) else {}
        return {"data": data, "meta": meta}

    def _metrics(self, params: dict[str, str]) -> dict[str, Any]:
        assert set(params) == {"query"}, sorted(params)
        q = json.loads(params["query"])
        assert q["view"] == "observations"
        t0, t1 = _parse_time(q["fromTimestamp"]), _parse_time(q["toTimestamp"])
        rows = [r for r in self.rows if t0 <= r.start < t1 and all(r.matches(c) for c in q["filters"])]
        dimensions = [d["field"] for d in q["dimensions"]]
        if "traceId" in dimensions:
            assert q.get("config", {}).get("row_limit"), "a traceId dimension needs row_limit"
            assert q.get("orderBy"), "a traceId dimension needs orderBy"
        granularity = (q.get("timeDimension") or {}).get("granularity")
        groups: dict[tuple[Any, ...], list[Row]] = defaultdict(list)
        for r in rows:
            bucket = (r.start.strftime(_GRANULARITY[granularity]),) if granularity else ()
            groups[(*bucket, *(r.column(d) for d in dimensions))].append(r)
        out: list[dict[str, Any]] = []
        for key, members in groups.items():
            item: dict[str, Any] = {}
            if granularity:
                item["time_dimension"] = key[0]
                key = key[1:]
            item.update(zip(dimensions, key, strict=True))
            for metric in q["metrics"]:
                item[f"{metric['aggregation']}_{metric['measure']}"] = _aggregate(members, metric)
            out.append(item)
        for order in reversed(q.get("orderBy") or []):
            out.sort(key=lambda item: item[order["field"]] or 0, reverse=order["direction"] == "desc")
        limit = q.get("config", {}).get("row_limit")
        return {"data": out[:limit] if limit else out}


def _aggregate(members: list[Row], metric: dict[str, str]) -> Any:
    measure, aggregation = metric["measure"], metric["aggregation"]
    if measure == "count":
        return len(members)
    if measure == "latency":
        values = [r.latency * 1000 for r in members if r.latency is not None]
        return sum(values) / len(values) if values else None
    if measure == "totalTokens":
        return sum(r.total_tokens for r in members)
    assert (measure, aggregation) == ("totalCost", "sum")
    costs = [r.total_cost for r in members if r.total_cost is not None]
    return sum(costs) if costs else None
