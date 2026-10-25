"""The read seam every read shares: its requests, retries and errors, and the row helpers: input and output,
tokens, kinds and producer metadata."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
from tai42_contract.monitoring import MonitoringError, SpanKind

from tai42_monitoring_langfuse.filters import trace_ids_clause
from tai42_monitoring_langfuse.measures import _measure_value
from tai42_monitoring_langfuse.observation_query import (
    EXPAND_METADATA,
    decode_io,
    observation_kind,
    producer_metadata,
    token_counts,
    total_tokens,
)

from ._fake_langfuse import FakeLangfuse

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
_PATH = "/api/public/v2/observations"


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The pauses between retries, taken without waiting."""
    taken: list[float] = []
    monkeypatch.setattr("time.sleep", taken.append)
    return taken


def _answer(status: int, body: bytes = b"", *, retry_after: str | None = None) -> httpx.Response:
    return httpx.Response(status, content=body, headers={} if retry_after is None else {"Retry-After": retry_after})


async def test_a_transient_answer_is_retried_after_a_backoff(fake: FakeLangfuse, reader, sleeps, caplog):
    fake.run("t1", T0)
    fake.fail(_answer(503))
    with caplog.at_level(logging.WARNING):
        trace = await reader.get_trace("t1")
    assert trace.id == "t1"
    assert len(fake.calls_of("observations")) == 2
    assert sleeps == [1.0]
    assert "503" in caplog.text
    assert _PATH in caplog.text


@pytest.mark.parametrize("status", [429, 408, 500, 502, 504])
async def test_each_transient_status_is_retried(fake: FakeLangfuse, reader, sleeps, status):
    fake.run("t1", T0)
    fake.fail(_answer(status))
    assert (await reader.get_trace("t1")).id == "t1"
    assert len(fake.calls_of("observations")) == 2


async def test_retry_after_in_seconds_sets_the_pause(fake: FakeLangfuse, reader, sleeps):
    fake.run("t1", T0)
    fake.fail(_answer(429, retry_after="3"))
    await reader.get_trace("t1")
    assert sleeps == [3.0]


async def test_retry_after_as_a_past_date_retries_at_once(fake: FakeLangfuse, reader, sleeps):
    fake.run("t1", T0)
    fake.fail(_answer(503, retry_after=format_datetime(T0, usegmt=True)))
    await reader.get_trace("t1")
    assert sleeps == [0.0]


async def test_an_unreadable_retry_after_is_refused(fake: FakeLangfuse, reader, sleeps):
    fake.fail(_answer(503, retry_after="soon"))
    with pytest.raises(MonitoringError, match="Retry-After 'soon'"):
        await reader.get_trace("t1")
    assert sleeps == []


async def test_a_retry_after_past_the_longest_pause_is_refused(fake: FakeLangfuse, reader, sleeps):
    fake.fail(_answer(503, retry_after="61"))
    with pytest.raises(MonitoringError, match="Retry-After '61'"):
        await reader.get_trace("t1")
    assert sleeps == []


async def test_a_transient_answer_past_the_retries_raises(fake: FakeLangfuse, reader, sleeps):
    fake.fail(_answer(503, b'{"message": "busy"}'), _answer(503), _answer(503, b'{"message": "still busy"}'))
    with pytest.raises(MonitoringError, match=rf"GET {_PATH} answered 503: still busy") as caught:
        await reader.get_trace("t1")
    assert type(caught.value) is MonitoringError
    assert len(fake.calls_of("observations")) == 3
    assert sleeps == [1.0, 2.0]


async def test_an_auth_failure_raises_at_once(fake: FakeLangfuse, reader, sleeps):
    fake.fail(_answer(401, b'{"message": "Invalid credentials"}'))
    with pytest.raises(MonitoringError, match=rf"GET {_PATH} answered 401: Invalid credentials"):
        await reader.get_trace("t1")
    assert len(fake.calls_of("observations")) == 1
    assert sleeps == []


@pytest.mark.parametrize("body", [b"bad filter", b'{"error": "bad filter"}'])
async def test_an_error_body_without_a_message_is_named_whole(fake: FakeLangfuse, reader, sleeps, body):
    fake.fail(_answer(400, body))
    with pytest.raises(MonitoringError, match="answered 400: ") as caught:
        await reader.get_trace("t1")
    assert str(caught.value).endswith(body.decode())


async def test_a_transport_failure_raises_chained(fake: FakeLangfuse, reader, sleeps):
    refused = httpx.ConnectError("connection refused")
    fake.fail(refused)
    with pytest.raises(MonitoringError, match="GET /api/public/v2/metrics failed: connection refused") as caught:
        await reader._metrics._metrics(await reader._metrics._active_client(), {"view": "observations"})
    assert caught.value.__cause__ is refused
    assert sleeps == []


@pytest.mark.parametrize(
    "body",
    [b"<html>gateway</html>", b'{"data": "rows"}', b'{"data": [{"id": "o"}], "meta": {}}', b'{"meta": {}}'],
)
async def test_a_body_that_does_not_decode_raises(fake: FakeLangfuse, reader, body):
    fake.fail(_answer(200, body))
    with pytest.raises(MonitoringError, match=rf"GET {_PATH} answered a body that does not decode") as caught:
        await reader.get_trace("t1")
    assert caught.value.__cause__ is not None


async def test_a_metrics_body_that_does_not_decode_raises(fake: FakeLangfuse, reader):
    fake.fail(_answer(200, b'{"rows": []}'))
    with pytest.raises(MonitoringError, match="GET /api/public/v2/metrics answered a body that does not decode"):
        await reader._metrics._metrics(await reader._metrics._active_client(), {"view": "observations"})


async def test_the_first_page_sends_no_cursor_and_the_next_sends_the_server_cursor(fake: FakeLangfuse, reader):
    for n in range(1001):
        fake.add(id=f"o{n}", trace_id="t1", start=T0)
    await reader.get_trace("t1")
    first, second = fake.calls_of("observations")
    assert "cursor" not in first
    assert second["cursor"] == "1000"
    assert first["limit"] == "1000"


async def test_expand_metadata_is_sent_only_when_asked(fake: FakeLangfuse, reader):
    fake.run("t1", T0)
    await reader.list_traces(from_timestamp=T0)
    assert fake.calls_of("observations")
    assert all("expandMetadata" not in params for params in fake.calls_of("observations"))
    fake.calls.clear()
    await reader.get_trace("t1")
    assert [params["expandMetadata"] for params in fake.calls_of("observations")] == [EXPAND_METADATA]


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [('{"a":1}', {"a": 1}), ("hello", "hello"), ("[1, 2]", [1, 2]), (None, None), ({"a": 1}, {"a": 1})],
)
def test_io_decodes_json_text_and_keeps_plain_text(raw, decoded):
    assert decode_io(raw) == decoded


@pytest.mark.parametrize(
    ("langfuse_type", "kind"),
    [
        ("GENERATION", SpanKind.LLM),
        ("EMBEDDING", SpanKind.LLM),
        ("TOOL", SpanKind.TOOL),
        ("RETRIEVER", SpanKind.TOOL),
        ("EVENT", SpanKind.EVENT),
        ("SPAN", SpanKind.CHAIN),
        ("AGENT", SpanKind.CHAIN),
        ("guardrail", SpanKind.CHAIN),
    ],
)
def test_each_langfuse_type_maps_to_a_neutral_kind(langfuse_type, kind):
    assert observation_kind(langfuse_type) is kind


@pytest.mark.parametrize(
    ("usage", "counts", "total"),
    [
        ({"input": 3, "output": 4, "total": 7}, (3, 4, 7), 7),
        ({"input": 3, "output": 4}, (3, 4, None), 7),
        ({"output": 0}, (None, 0, None), 0),
        ({}, (None, None, None), None),
        (None, (None, None, None), None),
    ],
)
def test_tokens_read_from_the_usage_details(usage, counts, total):
    assert tuple(token_counts(usage).values()) == counts
    assert total_tokens(usage) == total


def test_producer_metadata_reads_the_flat_attribute_keys():
    metadata = {
        "attributes.tai42.metadata": {"k": "v"},
        "attributes.tai42.step_role": "tool-call",
        "attributes.tai42.timing": {"ms": 3},
        "team": "blue",
    }
    assert producer_metadata(metadata, "o") == {"k": "v", "tai42.step_role": "tool-call", "tai42.timing": {"ms": 3}}


def test_producer_metadata_decodes_an_encoded_object():
    assert producer_metadata({"attributes.tai42.metadata": '{"k": 1}'}, "o") == {"k": 1}


@pytest.mark.parametrize("metadata", [None, {}, {"team": "blue"}])
def test_a_record_without_producer_metadata_has_none(metadata):
    assert producer_metadata(metadata, "o") is None


def test_a_cut_producer_value_is_refused():
    with pytest.raises(MonitoringError, match=r"undecodable tai42\.metadata attribute on observation o"):
        producer_metadata({"attributes.tai42.metadata": '{"k": "vvvv'}, "o")


def test_a_producer_value_that_is_no_object_is_refused():
    with pytest.raises(MonitoringError, match=r"malformed tai42\.metadata attribute on observation o"):
        producer_metadata({"attributes.tai42.metadata": [1]}, "o")


def test_the_expanded_metadata_keys_name_every_producer_key():
    assert EXPAND_METADATA.split(",") == ["attributes", "tai42.metadata", "tai42.step_role", "tai42.timing"]


def test_an_empty_trace_id_clause_is_refused():
    with pytest.raises(ValueError, match="at least one trace id"):
        trace_ids_clause([])


@pytest.mark.parametrize(
    ("cell", "value"),
    [
        (2, 2.0),
        (0.5, 0.5),
        (" 1.5 ", 1.5),
        ("", None),
        ("x", None),
        (True, None),
        (None, None),
        ("nan", None),
        (-1, None),
    ],
)
def test_a_metrics_cell_reads_as_a_non_negative_number(cell, value):
    assert _measure_value(cell) == value
