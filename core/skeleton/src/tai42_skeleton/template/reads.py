"""Record which stored templates the renders inside a block resolve or probe.

A block pushes a fresh set onto a context-local tuple; every recording point adds the id to
every set on the tuple, so nested blocks each see their own reads. A render runs on a worker
thread through ``asyncio.to_thread``, which copies the context: the copied tuple holds the same
set objects, so the thread's records land in the caller's sets. :class:`RecordingEnvironment` is the
sandboxed Jinja environment whose template lookups record the names they resolve.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from jinja2 import Template as JinjaTemplate
from jinja2.sandbox import SandboxedEnvironment

_READS: ContextVar[tuple[set[str], ...]] = ContextVar("tai42_template_reads", default=())


@contextmanager
def template_reads() -> Iterator[set[str]]:
    """Yield the set every render inside the block records its template ids into."""
    reads: set[str] = set()
    token = _READS.set((*_READS.get(), reads))
    try:
        yield reads
    finally:
        _READS.reset(token)


def record_template_read(template_id: str) -> None:
    """Add ``template_id`` to the set of every enclosing :func:`template_reads` block."""
    for reads in _READS.get():
        reads.add(template_id)


class RecordingEnvironment(SandboxedEnvironment):
    """The sandboxed environment that records every template name a render resolves.

    ``{% include %}``, ``{% extends %}`` and ``{% import %}`` reach their targets through
    these two public methods (``get_or_select_template`` delegates to them), so each target
    — a cache hit or a load, found or missing — is recorded before it resolves.
    """

    def get_template(self, name: Any, parent: str | None = None, globals: Any = None) -> JinjaTemplate:  # noqa: A002 - jinja's own parameter name
        """Record ``name`` (a template name, not an already-loaded template), then resolve it."""
        if isinstance(name, str):
            record_template_read(name)
        return super().get_template(name, parent, globals)

    def select_template(self, names: Any, parent: str | None = None, globals: Any = None) -> JinjaTemplate:  # noqa: A002 - jinja's own parameter name
        """Record every name in ``names``, then resolve the first that exists."""
        if not isinstance(names, str):
            for candidate in names:
                if isinstance(candidate, str):
                    record_template_read(candidate)
        return super().select_template(names, parent, globals)
