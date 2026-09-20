"""Request DTOs, status/config constants, and the run clock for tool-run operations."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

# The machine-readable code + message the tool-run OFF refusal carries when the
# store is unconfigured. Hoisted so the submit refusal reads one way.
_NOT_CONFIGURED_CODE = "tool-runs-not-configured"
_NOT_CONFIGURED_MESSAGE = "the tool-run store is not configured: set TAI_TOOL_RUNS_REDIS_URL (or TAI_DEFAULT_REDIS_URL)"

# The default cancellation reason recorded on a run cancelled by a drain — overridden
# per drain caller (epoch retire vs process shutdown) so the run's ``error`` names the
# real cause rather than always claiming a shutdown.
_DEFAULT_CANCEL_REASON = "the tool-run was cancelled before it completed"

_RUNNING = "running"
_SUCCEEDED = "succeeded"
_FAILED = "failed"
_LOST = "lost"
# A run whose tool async-parked (returned a ``SuspendedInteraction``): a terminal record that
# did NOT succeed — the deferred answer is delivered out of band by the tool's own resumer, so
# recording ``succeeded`` over the unfinished run would be a lie. GENERIC: any parking tool.
_PARKED = "parked"

# The platform-generic registration meta key a consumer sets to opt one of its runs
# into crash-resume (``meta={"tai42/crash_resume": True}``), the existing ``tai42/*``
# meta convention. The skeleton reads it and stores a generic bool; it names no consumer.
_CRASH_RESUME_META_KEY = "tai42/crash_resume"


def _now() -> datetime:
    return datetime.now(UTC)


class ToolRunSubmission(BaseModel):
    """A background tool-run submission: the ``tool_name`` and its keyword ``arguments``.

    Mirrors the shape ``read_tool_call`` enforces at runtime.
    """

    tool_name: str = Field(min_length=1, description="Registered tool name.")
    arguments: dict[str, object] = Field(default_factory=dict, description="Tool keyword arguments.")


class ToolRunsListQuery(BaseModel):
    """The per-tool run listing's ``?tool_name=`` query.

    ``tool_name`` is REQUIRED — a client generated without it calls the door with
    no tool to list and is answered 400. Spec metadata only — the door parses its
    query at the HTTP edge.
    """

    tool_name: str = Field(min_length=1, description="The registered tool whose recent runs to list.")
