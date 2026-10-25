"""The observations list and metrics calls every read surface goes through, and the row mapping they share.

Every call is a ``GET`` on Langfuse's public REST API v2, fetched and decoded off the event loop. A list call
sends its clauses in the ``filter`` JSON only, and a walk follows ``meta.cursor`` until the server omits it. The
rows decode into the plugin's own models: a window read decodes up to 52,000 rows, and the generated Langfuse SDK
client rewrites every row's keys through type reflection at about 0.4 s per 1,000 rows.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import time
from collections.abc import AsyncGenerator, Mapping
from datetime import datetime
from functools import partial
from typing import Any

import httpx
import orjson
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel
from tai42_contract.monitoring import (
    PROMOTED_METADATA_KEYS,
    STEP_ROLE_METADATA_KEY,
    TIMING_METADATA_KEY,
    MonitoringError,
    SpanKind,
)
from tai42_kit.monitoring.otel.attributes import METADATA as PRODUCER_METADATA_ATTRIBUTE

from tai42_monitoring_langfuse.filters import Clause
from tai42_monitoring_langfuse.query_base import WALK_LIMIT, _LangfuseQuery

_log = logging.getLogger(__name__)

_OBSERVATIONS_PATH = "/api/public/v2/observations"
_METRICS_PATH = "/api/public/v2/metrics"
# The server stores a span attribute ``k`` under the flat metadata key ``attributes.k``.
_ATTRIBUTE_KEY_PREFIX = "attributes."
# The metadata keys read back untruncated: the attribute catch-all and the producer keys.
EXPAND_METADATA = ",".join(
    ("attributes", PRODUCER_METADATA_ATTRIBUTE, STEP_ROLE_METADATA_KEY, TIMING_METADATA_KEY),
)
# The field groups of a full observation.
FULL_FIELDS = "core,basic,time,io,metadata,model,usage,metrics,trace_context"

# The answers retried, and how: up to ``_MAX_RETRIES`` more requests, each after the answer's ``Retry-After``
# (at most ``_MAX_PAUSE_SECONDS``) or else a doubling backoff from ``_FIRST_BACKOFF_SECONDS``.
_RETRIED_STATUSES = frozenset({408, 429})
_MAX_RETRIES = 2
_FIRST_BACKOFF_SECONDS = 1.0
_MAX_PAUSE_SECONDS = 60.0


class _WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="ignore", frozen=True)


class ObservationRow(_WireModel):
    """One observation row, with the fields the reads use; a field group not requested leaves its fields unset."""

    id: str
    trace_id: str
    start_time: datetime
    end_time: datetime | None = None
    parent_observation_id: str | None = None
    type: str
    name: str | None = None
    level: str | None = None
    status_message: str | None = None
    is_root_observation: bool | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    model: str | None = None
    usage_details: dict[str, int] | None = None
    total_cost: float | None = None
    latency: float | None = None
    tags: list[str] | None = None
    trace_name: str | None = None


class _PageMeta(_WireModel):
    cursor: str | None = None


class ObservationsPage(_WireModel):
    """One page of the observations list and the cursor of the next, ``None`` on the last."""

    data: list[ObservationRow]
    meta: _PageMeta

    @property
    def cursor(self) -> str | None:
        """The next page's cursor."""
        return self.meta.cursor


class MetricsPage(_WireModel):
    """The rows of one metrics query."""

    data: list[dict[str, Any]]


def _retry_pause(response: httpx.Response, retry: int) -> float:
    """Seconds to wait before retry number ``retry`` (from 0): the answer's ``Retry-After``, else the backoff."""
    header = response.headers.get("retry-after")
    if header is None:
        return _FIRST_BACKOFF_SECONDS * 2**retry
    text = header.strip()
    if text.isdigit():
        pause = float(text)
    else:
        try:
            pause = max(0.0, email.utils.parsedate_to_datetime(text).timestamp() - time.time())
        except (TypeError, ValueError) as exc:
            raise _upstream_error(response, f"answered {response.status_code} with Retry-After {header!r}") from exc
    if pause > _MAX_PAUSE_SECONDS:
        raise _upstream_error(response, f"answered {response.status_code} with Retry-After {header!r}")
    return pause


def _server_message(response: httpx.Response) -> str:
    """The error answer's ``message`` when its body is a JSON object carrying one, else the body as text."""
    try:
        body = orjson.loads(response.content)
    except orjson.JSONDecodeError:
        return response.text
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return response.text


def _upstream_error(response: httpx.Response, what: str) -> MonitoringError:
    return MonitoringError(f"Langfuse GET {response.request.url.path} {what}")


def _get(client: httpx.Client, path: str, params: dict[str, str | int]) -> bytes:
    """The body of a ``GET`` answered 2xx, retrying 408, 429 and 5xx answers; every failure raises."""
    retry = 0
    while True:
        try:
            response = client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise MonitoringError(f"Langfuse GET {path} failed: {exc}") from exc
        status = response.status_code
        if response.is_success:
            return response.content
        if not (status in _RETRIED_STATUSES or status >= 500) or retry == _MAX_RETRIES:
            raise _upstream_error(response, f"answered {status}: {_server_message(response)}")
        pause = _retry_pause(response, retry)
        retry += 1
        _log.warning("Langfuse GET %s answered %s; retry %s of %s in %.1fs", path, status, retry, _MAX_RETRIES, pause)
        time.sleep(pause)


def _decode[Page: BaseModel](model: type[Page], path: str, body: bytes) -> Page:
    try:
        return model.model_validate(orjson.loads(body))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        raise MonitoringError(f"Langfuse GET {path} answered a body that does not decode: {exc}") from exc


def _fetch[Page: BaseModel](client: httpx.Client, model: type[Page], path: str, params: dict[str, str | int]) -> Page:
    return _decode(model, path, _get(client, path, params))


class ObservationQuery(_LangfuseQuery):
    """The observations list and metrics calls of Langfuse's public REST API v2."""

    async def _list_page(
        self,
        client: httpx.Client,
        *,
        fields: str,
        clauses: list[Clause],
        cursor: str | None = None,
        expand_metadata: bool = False,
        limit: int = WALK_LIMIT,
    ) -> tuple[list[ObservationRow], str | None]:
        params: dict[str, str | int] = {"fields": fields, "filter": json.dumps(clauses), "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if expand_metadata:
            params["expandMetadata"] = EXPAND_METADATA
        page = await asyncio.to_thread(partial(_fetch, client, ObservationsPage, _OBSERVATIONS_PATH, params))
        return page.data, page.cursor

    async def _walk(
        self, client: httpx.Client, *, fields: str, clauses: list[Clause], expand_metadata: bool = False
    ) -> AsyncGenerator[ObservationRow]:
        """Every row the clauses match, newest start first, one list call per page as the consumer reads on."""
        cursor: str | None = None
        previous: datetime | None = None
        while True:
            rows, cursor = await self._list_page(
                client, fields=fields, clauses=clauses, cursor=cursor, expand_metadata=expand_metadata
            )
            for row in rows:
                if previous is not None and row.start_time > previous:
                    raise MonitoringError("Langfuse returned observations out of startTime order")
                previous = row.start_time
                yield row
            if not cursor:
                return

    async def _walk_all(
        self, client: httpx.Client, *, fields: str, clauses: list[Clause], expand_metadata: bool = False
    ) -> list[ObservationRow]:
        walk = self._walk(client, fields=fields, clauses=clauses, expand_metadata=expand_metadata)
        return [row async for row in walk]

    async def _metrics(self, client: httpx.Client, query: dict[str, Any]) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {"query": json.dumps(query)}
        page = await asyncio.to_thread(partial(_fetch, client, MetricsPage, _METRICS_PATH, params))
        return page.data


# Langfuse observation type -> neutral kind; any type not named here is a CHAIN.
_TYPE_TO_KIND: dict[str, SpanKind] = {
    "GENERATION": SpanKind.LLM,
    "EMBEDDING": SpanKind.LLM,
    "TOOL": SpanKind.TOOL,
    "RETRIEVER": SpanKind.TOOL,
    "EVENT": SpanKind.EVENT,
}


def observation_kind(obs_type: str) -> SpanKind:
    """The neutral kind of a Langfuse observation type."""
    return _TYPE_TO_KIND.get(obs_type.upper(), SpanKind.CHAIN)


def decode_io(value: Any) -> Any:
    """An input or output value as the producer wrote it: a JSON string parsed, any other string kept as text."""
    if not isinstance(value, str):
        return value
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError:
        return value


def token_counts(usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    """The token counts from Langfuse's usage keys ``input`` / ``output`` / ``total``; ``None`` when absent."""
    usage = usage or {}

    def _count(key: str) -> int | None:
        value = usage.get(key)
        return None if value is None else int(value)

    return {"input_tokens": _count("input"), "output_tokens": _count("output"), "total_tokens": _count("total")}


def total_tokens(usage: Mapping[str, Any] | None) -> int | None:
    """An observation's total tokens: ``total``, else ``input`` + ``output``; ``None`` when it reports none."""
    counts = token_counts(usage)
    if counts["total_tokens"] is not None:
        return counts["total_tokens"]
    if counts["input_tokens"] is None and counts["output_tokens"] is None:
        return None
    return (counts["input_tokens"] or 0) + (counts["output_tokens"] or 0)


def producer_metadata(metadata: Mapping[str, Any] | None, observation_id: str) -> dict[str, Any] | None:
    """The record's producer metadata: the decoded ``tai42.metadata`` object plus the promoted keys.

    Read from the flat ``attributes.<key>`` metadata keys; ``None`` when the record carries none. A value that does
    not decode to an object raises ``MonitoringError``.
    """
    if not metadata:
        return None
    producer: dict[str, Any] = {}
    encoded = metadata.get(_ATTRIBUTE_KEY_PREFIX + PRODUCER_METADATA_ATTRIBUTE)
    if isinstance(encoded, str):
        try:
            encoded = orjson.loads(encoded)
        except orjson.JSONDecodeError as exc:
            raise MonitoringError(
                f"undecodable {PRODUCER_METADATA_ATTRIBUTE} attribute on observation {observation_id}"
            ) from exc
    if encoded is not None:
        if not isinstance(encoded, Mapping):
            raise MonitoringError(f"malformed {PRODUCER_METADATA_ATTRIBUTE} attribute on observation {observation_id}")
        producer.update(encoded)
    for key in PROMOTED_METADATA_KEYS:
        flat = _ATTRIBUTE_KEY_PREFIX + key
        if flat in metadata:
            producer[key] = metadata[flat]
    return producer or None
