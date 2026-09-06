"""Harness self-test: every feature keyspace is namespaced per stack.

The per-stack logical-DB isolation does NOT isolate a stack whose DB index is
reused while a long-lived stack still holds keys under it (the bus namespace
exists for the same reason — pub/sub channels and presence keys are
server-global). So every feature Redis store a foreign process could reach on a
shared DB — one carrying an active cross-stack mutator (the interactions expiry
reaper) or one addressed by a collision-prone logical name (a hook name, a
connector slug, a rate-limit identity, a per-tool run index) — has its key prefix
namespaced by the same per-stack id the bus uses, and the per-backend queue/beat
env is namespaced the same way, so two stacks can never share a key or a queue.
"""

from __future__ import annotations

from dataclasses import fields, replace

from tai42_e2e.manifests import _redis_feature_env
from tai42_e2e.stack import StackResources


def _resources(*, bus_namespace: str, broker_url: str | None = None) -> StackResources:
    return StackResources(
        redis_idx=1,
        redis_url="redis://127.0.0.1:6379/1",
        probe_redis_url="redis://127.0.0.1:6379/0",
        pg_host="127.0.0.1",
        pg_port=5432,
        pg_user="postgres",
        pg_password="postgres",
        pg_db="tai42_e2e_probe",
        storage_root="/tmp/e2e-storage",
        bus_namespace=bus_namespace,
        broker_url=broker_url,
    )


def test_interactions_key_prefix_is_namespaced_by_the_stack() -> None:
    env = _redis_feature_env(_resources(bus_namespace="tai42_e2e_abc123"))
    prefix = env["INTERACTIONS_KEY_PREFIX"]
    # Carries the stack's own namespace, so its keys never collide with the bare
    # ``interactions:`` default a co-located foreign stack would use.
    assert prefix.startswith("tai42_e2e_abc123:")
    assert "interactions:" in prefix


def test_two_stacks_never_share_the_interactions_keyspace() -> None:
    a = _redis_feature_env(_resources(bus_namespace="tai42_e2e_aaa"))["INTERACTIONS_KEY_PREFIX"]
    b = _redis_feature_env(_resources(bus_namespace="tai42_e2e_bbb"))["INTERACTIONS_KEY_PREFIX"]
    assert a != b


# Every feature keyspace prefix ``_redis_feature_env`` emits (a ``*_KEY_PREFIX``
# or a bare ``*_PREFIX``), each of which a foreign process on a re-leased logical
# DB could otherwise collide with — an active reaper over a shared range, or a
# key addressed by a collision-prone logical name.
def _emitted_prefix_keys(env: dict[str, str]) -> list[str]:
    return [k for k in env if k.endswith(("_KEY_PREFIX", "_PREFIX"))]


def test_every_feature_prefix_is_namespaced_by_the_stack() -> None:
    ns = "tai42_e2e_abc123"
    env = _redis_feature_env(_resources(bus_namespace=ns))
    keys = _emitted_prefix_keys(env)
    # The set the chokepoint must cover — a new feature Redis store added to
    # ``_redis_feature_env`` without a namespaced prefix fails this test.
    assert set(keys) == {
        "INTERACTIONS_KEY_PREFIX",
        "TAI_TOOL_RUNS_KEY_PREFIX",
        "TAI_RATE_LIMIT_KEY_PREFIX",
        "HOOKS_PREFIX",
        "SUB_MCP_PREFIX",
        "CONNECTOR_STORE_KEY_PREFIX",
    }
    for key in keys:
        assert env[key].startswith(f"{ns}:"), f"{key}={env[key]!r} is not namespaced by the stack"


def test_two_stacks_never_share_any_feature_prefix() -> None:
    a = _redis_feature_env(_resources(bus_namespace="tai42_e2e_aaa"))
    b = _redis_feature_env(_resources(bus_namespace="tai42_e2e_bbb"))
    for key in _emitted_prefix_keys(a):
        assert a[key] != b[key], f"{key} is shared across two stacks"


def test_backend_queue_env_is_stack_unique() -> None:
    """The backend queue/beat env each stack's worker binds carries the per-stack
    token, so a leaked worker on a re-leased DB index cannot consume this stack's
    jobs (arq/rq) or fire its schedules (celery RedBeat). Celery's task queue is
    isolated by its per-stack RabbitMQ vhost, so it namespaces the RedBeat store
    rather than the queue."""
    from tai42_e2e.variants import ArqVariant, RqVariant

    for variant, env_key in ((ArqVariant(), "ARQ_QUEUE_NAME"), (RqVariant(), "RQ_QUEUE_NAME")):
        a = variant.feature_env(_resources(bus_namespace="tai42_e2e_aaa"))[env_key]
        b = variant.feature_env(_resources(bus_namespace="tai42_e2e_bbb"))[env_key]
        assert a.startswith("tai42_e2e_aaa:"), f"{env_key}={a!r} not namespaced"
        assert a != b, f"{env_key} shared across two stacks"


def test_celery_redbeat_prefix_is_stack_unique() -> None:
    from tai42_e2e.variants import CeleryVariant

    variant = CeleryVariant()
    a = variant.feature_env(_resources(bus_namespace="tai42_e2e_aaa", broker_url="amqp://h/tai42_e2e_aaa"))
    b = variant.feature_env(_resources(bus_namespace="tai42_e2e_bbb", broker_url="amqp://h/tai42_e2e_bbb"))
    assert a["CELERY_REDBEAT_KEY_PREFIX"].startswith("tai42_e2e_aaa:")
    assert a["CELERY_REDBEAT_KEY_PREFIX"] != b["CELERY_REDBEAT_KEY_PREFIX"]


def test_conversations_prefix_is_namespaced_on_every_stack_that_mounts_it() -> None:
    """The conversations delivery sweep is a periodic cross-record mutator, so its
    keyspace carries the stack namespace on every manifest that wires the
    conversations redis backend — a foreign serve on a shared DB never re-drives
    or confirms this stack's deliveries."""
    from tai42_e2e.manifests import build_agent_route_park_stack, build_bridge_stack
    from tai42_e2e.variants import BACKENDS, IDENTITIES, STORAGES, Variants

    variants = Variants(backend=BACKENDS["arq"], identity=IDENTITIES["fixture"], storage=STORAGES["local"])
    res = _resources(bus_namespace="tai42_e2e_abc123")
    stubs = {
        f.name: "http://127.0.0.1:1" for f in fields(StackResources) if f.name.endswith(("_stub_base", "_api_base_url"))
    }
    bridge_res = replace(res, **stubs)
    park_res = replace(res, checkpoint_redis_url="redis://127.0.0.1:6379/2")
    for build, r in ((build_bridge_stack, bridge_res), (build_agent_route_park_stack, park_res)):
        env = build(r, variants).env
        assert "CONVERSATIONS_REDIS_URL" in env, f"{build.__name__} wires no conversations backend"
        assert env["CONVERSATIONS_PREFIX"] == "tai42_e2e_abc123:conversations"
