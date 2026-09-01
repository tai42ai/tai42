"""``TAI_RUNS_INDEX_*`` config for the runs index.

The store itself binds the shared skeleton Postgres component (no connection fields
of its own — it rides ``component_store_settings(SKELETON_COMPONENT)`` like the
versioned-document store), so this config carries only the feature knobs: the opt-in
retention window the prune consumes, and the list page-size cap.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class RunIndexSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="TAI_RUNS_INDEX_")

    # Opt-in retention window in DAYS. ``None`` (the default) keeps every run row
    # forever — retention is off unless a deployment sets it, mirroring the
    # checkpoint-retention ``checkpoint_ttl_minutes`` knob (unset = kept forever). The
    # prune deletes rows whose ``started_at`` is older than this; a set value must be
    # positive.
    retention_days: int | None = Field(default=None, ge=1)

    # The list door caps a requested page size to this: it deliberately CLAMPS an
    # oversized page rather than refusing it, so a huge ``pageSize`` cannot pull an
    # unbounded slice — the same forgiving-list-door posture as the observability run
    # list's ``PAGE_CHUNK`` cap (only malformed ``< 1`` paging is rejected, there and
    # here). Must be positive.
    max_page_size: int = Field(default=200, gt=0)


@settings_cache
def run_index_settings() -> RunIndexSettings:
    return RunIndexSettings()
