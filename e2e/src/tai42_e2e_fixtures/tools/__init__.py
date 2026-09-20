"""Probe tools the harness observes the system under test through.

Selected by a ``tools:`` manifest entry at the dotted path
``tai42_e2e_fixtures.tools``. Importing this package imports every submodule, so
every ``@tai42_app.tools.tool`` probe registers on import. Probes never mock —
each exposes an in-process observable (a return value or an ``e2e_record`` Redis
side effect) so a test can read what actually happened inside a real
server/worker process."""

from __future__ import annotations

from tai42_e2e_fixtures.tools import basic, connector, overlap, park, sandbox, tool_target

__all__ = ["basic", "connector", "overlap", "park", "sandbox", "tool_target"]
