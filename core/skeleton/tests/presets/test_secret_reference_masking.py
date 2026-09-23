"""A preset-baked ``!ENV`` reference, resolved at bind, is masked at the trace
recorder door.

The ``monitor`` builtin wraps a standalone tool call in a ``SpanKind.TOOL`` span and
records ``mask_secrets(args/kwargs)`` on entry and ``mask_secrets(result)`` on exit.
A preset over a base tool bakes a ``!ENV ${VAR}`` reference into a nested
``fixed_kwargs`` leaf; running its monitor branch forwards the resolved value to the
base tool, whose return rides back through the span — masked to the placeholder,
never the resolved value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from tai42_contract.monitoring import SpanKind
from tai42_contract.secrets import SecretValue, unwrap_secrets

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.monitoring import (
    NoOpMonitoring,
    NoOpReader,
    NoOpSpan,
    NoOpWriter,
    init_monitoring,
    reset_monitoring,
)

from ._manager_fixtures import FakeVersioningPg

_VAR = "PRESET_MONITOR_SECRET_VAR"
_RESOLVED = "resolved-monitor-credential"
_MARKER = f"!ENV ${{{_VAR}}}"


class _RecordingSpan(NoOpSpan):
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    def update(self, *, output: Any = None, **_: Any) -> None:
        if output is not None:
            self.outputs.append(output)


class _RecordingWriter(NoOpWriter):
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def start_span(self, *, name: str, kind: SpanKind, input_: Any = None, **_: Any):
        from contextlib import contextmanager

        span = _RecordingSpan()
        self.spans.append({"name": name, "kind": kind, "input": input_, "span": span})

        @contextmanager
        def _scope() -> Iterator[_RecordingSpan]:
            yield span

        return _scope()


class _RecordingMonitoring(NoOpMonitoring):
    def __init__(self) -> None:
        self._writer = _RecordingWriter()

    @property
    def writer(self) -> _RecordingWriter:
        return self._writer

    @property
    def reader(self) -> NoOpReader:
        return NoOpReader()


_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures", "tai42_skeleton.extensions.builtin.monitor"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["payload_tool"]}],
}


@pytest.fixture(autouse=True)
def _monitoring() -> Iterator[None]:
    reset_monitoring()
    yield
    reset_monitoring()


def test_monitor_span_masks_a_preset_baked_reference(pg: FakeVersioningPg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_VAR, _RESOLVED)
    backend = _RecordingMonitoring()
    init_monitoring(backend)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate(_MANIFEST)):
            # A preset baking a nested ``!ENV`` reference, branched by ``monitor``.
            await app.preset_manager.register(
                "ref_payload", "payload_tool", {"payload": {"token": _MARKER}}, [["monitor"]], "Ref payload"
            )
            result = await app.tools.run_tool("ref_payload_monitor", {})

        # The base tool received the resolved value wrapped as a ``SecretValue`` — the
        # in-process dispatch carries the wrapper out intact — proving the reference
        # resolved at bind and reached the tool.
        assert isinstance(result["payload"]["token"], SecretValue)
        assert unwrap_secrets(result) == {"payload": {"token": _RESOLVED}, "tag": "t"}

        # Exactly one standalone TOOL span, and its recorded OUTPUT masks the resolved value.
        assert len(backend.writer.spans) == 1
        span = backend.writer.spans[0]
        assert span["kind"] is SpanKind.TOOL
        assert span["span"].outputs == [{"payload": {"token": "[secret]"}, "tag": "t"}]
        assert _RESOLVED not in repr(span["span"].outputs)
        # The recorded INPUT carries no secret either (the reference is a hidden baked
        # constant, never an exposed argument the recorder sees).
        assert _RESOLVED not in repr(span["input"])

    asyncio.run(run())
