"""Per-stack broker isolation on the celery leg: two concurrent stacks each
lease their own live AMQP vhost, and every vhost dies with its own stack (a
leaked vhost is a teardown RuntimeError, symmetric to a leaked Postgres db).

Skips on the Redis-broker backends (arq/rq): they ride the shared Redis and
lease no broker, so there is no per-stack broker resource to isolate."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.manifests import build_core_stack
from tai42_e2e.rabbitx import RabbitAdmin
from tai42_e2e.stack import Infra, TaiStack


def test_concurrent_stacks_get_isolated_vhosts_that_die_with_them(
    fresh_stack: Callable[..., TaiStack], infra: Infra
) -> None:
    if infra.variants.backend.name != "celery":
        pytest.skip(
            f"{infra.variants.backend.name}: rides the shared Redis and leases no broker, "
            "so there is no per-stack broker vhost to isolate"
        )

    admin = RabbitAdmin(infra.settings.rabbitmq_management_url)

    stack_a = fresh_stack(build_core_stack)
    stack_b = fresh_stack(build_core_stack)
    lease_a = stack_a.resources.broker_lease
    lease_b = stack_b.resources.broker_lease
    assert lease_a is not None, "the celery stack leased no broker vhost"
    assert lease_b is not None, "the celery stack leased no broker vhost"

    # Two concurrent stacks get distinct, live vhosts — no cross-talk.
    assert lease_a.vhost != lease_b.vhost, "two concurrent stacks shared one broker vhost"
    assert admin.vhost_exists(lease_a.vhost), "stack A's leased vhost is not present on the broker"
    assert admin.vhost_exists(lease_b.vhost), "stack B's leased vhost is not present on the broker"

    # Each vhost dies with its own stack, and only its own.
    stack_a.teardown()
    assert not admin.vhost_exists(lease_a.vhost), "stack A's vhost outlived its teardown (leak)"
    assert admin.vhost_exists(lease_b.vhost), "stack B's vhost died with a sibling's teardown"
