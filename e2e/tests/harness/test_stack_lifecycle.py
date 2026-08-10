"""Harness self-tests — keep the harness honest: a stack boots and tears down
leak-free, and no profile's env ever carries ``PROMETHEUS_MULTIPROC_DIR`` (the
C2 hard rule)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import ports
from tai42_e2e.manifests import (
    build_agents_redis_stack,
    build_agents_stack,
    build_auth_stack,
    build_bare_stack,
    build_channel_stack,
    build_connectors_stack,
    build_core_stack,
    build_embed_stack,
    build_extensions_stack,
    build_minimal_stack,
    build_monitoring_stack,
    build_payments_stack,
    build_replicas_stack,
    build_schedule_stack,
    build_studio_stack,
)
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants, resolve_variants

# Every ``build_*_stack`` in ``manifests``: the multiproc-env sweep below asserts the
# no-``PROMETHEUS_MULTIPROC_DIR`` rule for EVERY profile the harness can render, so a
# profile that leaked it could not slip past by not being listed.
_ALL_BUILDERS: list[Callable[[StackResources, Variants], StackConfig]] = [
    build_minimal_stack,
    build_bare_stack,
    build_core_stack,
    build_embed_stack,
    build_replicas_stack,
    build_schedule_stack,
    build_auth_stack,
    build_agents_stack,
    build_agents_redis_stack,
    build_studio_stack,
    build_connectors_stack,
    build_extensions_stack,
    build_monitoring_stack,
    build_channel_stack,
    build_payments_stack,
]


@pytest.mark.backendless
async def test_stack_boots_and_tears_down_leak_free(fresh_stack: Callable[..., TaiStack]) -> None:
    # The minimal stack this boots runs no backend worker, so it exercises no
    # backend seam and buys nothing on the non-default legs.
    stack = fresh_stack(build_minimal_stack)
    assert all(handle.is_running() for handle in stack._procs.values())
    allocated = list(stack.app_ports) + ([stack.metrics_port] if stack.metrics_port else [])
    stack.teardown()
    # After teardown every port is free and no child survives.
    for port in allocated:
        assert ports.is_free(port), f"port {port} still bound after teardown"
    assert not any(handle.is_running() for handle in stack._procs.values())


def test_harness_never_sets_multiproc_env() -> None:
    # A sentinel carrying EVERY coordinate any profile needs — including the ones
    # only some backends/profiles read (a broker URL for celery, the checkpoint
    # Redis for the agents-redis profile, the Studio dist) — so the sweep renders
    # all thirteen profiles on every backend leg rather than erroring on one.
    sentinel = StackResources(
        redis_idx=1,
        redis_url="redis://127.0.0.1:6379/1",
        probe_redis_url="redis://127.0.0.1:6379/0",
        pg_host="127.0.0.1",
        pg_port=5432,
        pg_user="postgres",
        pg_password="postgres",
        pg_db="tai42_e2e_probe",
        storage_root="/tmp/e2e-storage",
        llm_base_url="http://127.0.0.1:1/v1",
        gh_webhook_secret="x",
        connectors_kek="k",
        connectors_state_hmac_key="h",
        idp_base_url="http://127.0.0.1:1",
        langfuse_host="http://127.0.0.1:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        broker_url="amqp://guest:guest@127.0.0.1:5672/tai42_e2e_probe",
        checkpoint_redis_idx=1,
        checkpoint_redis_url="redis://127.0.0.1:6380/1",
        studio_dist_path="/tmp/e2e-studio-dist",
        telegram_api_base_url="http://127.0.0.1:1",
        slack_api_base_url="http://127.0.0.1:1/api",
        twilio_api_base_url="http://127.0.0.1:1",
    )
    variants = resolve_variants(HarnessSettings())
    for builder in _ALL_BUILDERS:
        config = builder(sentinel, variants)
        assert "PROMETHEUS_MULTIPROC_DIR" not in config.env, f"{config.name} env leaks PROMETHEUS_MULTIPROC_DIR"
