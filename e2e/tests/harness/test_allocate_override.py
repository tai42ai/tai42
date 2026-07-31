"""Harness self-test for ``allocate_resources``' connection-endpoint overrides: a
``redis_host``/``redis_port``/``pg_host``/``pg_port`` override reaches the rendered
stack env, while the harness still allocates the real logical Redis DB and PG clone
and the rendered ``redis_url`` still selects the index it reserved."""

from __future__ import annotations

from pathlib import Path

import pytest

from tai42_e2e.harness import allocate_resources
from tai42_e2e.manifests import build_core_stack
from tai42_e2e.stack import Infra

# Pure harness self-test: it renders env, boots nothing, and exercises no backend
# seam, so running it under every backend leg buys nothing.
pytestmark = pytest.mark.backendless


def test_override_endpoints_reach_the_rendered_env(infra: Infra, tmp_path: Path) -> None:
    resources = allocate_resources(
        infra,
        tmp_path,
        redis_host="127.0.0.1",
        redis_port=59999,
        pg_host="127.0.0.1",
        pg_port=59998,
    )
    try:
        # The SUT's endpoints are the overridden ones, while the harness-side
        # allocation (the logical DB index, the PG database name) is untouched —
        # and the URL selects exactly the index the allocator reserved, so the two
        # can never disagree.
        assert resources.redis_url == f"redis://127.0.0.1:59999/{resources.redis_idx}"
        assert resources.redis_idx in range(1, 16)
        assert resources.pg_host == "127.0.0.1"
        assert resources.pg_port == 59998
        # The probe channel stays on the REAL Redis: the harness must still read
        # probe records while the SUT's own endpoint is severed.
        assert resources.probe_redis_url == infra.redis.url_for(0)

        # The override reaches the feature env every builder renders: the SUT's
        # feature Redis URL and PG coordinates point at the given endpoints.
        env = build_core_stack(resources, infra.variants).env
        assert env["ACCESS_CONTROL_REDIS_URL"] == resources.redis_url
        assert env["TAI_TOOL_RUNS_REDIS_URL"] == resources.redis_url
        assert env["VERSIONING_STORE_PG_HOST"] == "127.0.0.1"
        assert env["VERSIONING_STORE_PG_PORT"] == "59998"
    finally:
        # No TaiStack owns these, so reap the real resources the allocator made.
        if resources.broker_lease is not None:
            resources.broker_lease.release()
        infra.pg.drop_stack_db(resources.pg_db)
        infra.redis.release_db(resources.redis_idx)
