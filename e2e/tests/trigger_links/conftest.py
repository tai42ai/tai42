"""Fixtures for the trigger-link journey suite.

The Task-1 journey tests run on a DEDICATED stack (``trigger_stack``): an
access-control-ENABLED accounts clone (so the per-tag grant matrix has real
enforcement and the accounts provider serves the role/user seeding the matrix
needs) in the REPLICAS topology (a sibling port for the cross-replica fire),
with the redis hooks backend (trigger links 501 in-memory by design), the
webhook-verifier lifecycle modules (the verifier bind validates the verifier NAME
against the registry), the backup router (the round-trip legs), and the
``trigger_*`` rate-limit family raised high so the whole suite firing from one
localhost bucket never trips the default-ON limiter.

Under access control the resolver door ``/trigger/{token}`` is ROUTE-TABLE
public: the guard resolves public/protected from the route table alone, so the
seed pins ``/trigger/*`` to the public marker and excludes it from the catch-all
protected pattern. The authed CRUD routes stay under the catch-all (an
unauthenticated call is denied, the grant matrix then decides).

The ``_rbac_support`` role helpers live in a sibling test directory; pytest's
prepend import mode only puts a test file's OWN directory on ``sys.path``, so that
directory is added here so the suite imports the same helpers rather than
duplicating them.
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import redis

from tai42_e2e import diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.harness import seed_bootstrap_key, seed_route_rows
from tai42_e2e.manifests import build_accounts_stack
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants

_TESTS_DIR = Path(__file__).resolve().parent.parent
for _sibling in ("access_control",):
    _path = str(_TESTS_DIR / _sibling)
    if _path not in sys.path:
        sys.path.insert(0, _path)


# -- the dedicated stack builder ---------------------------------------


def build_trigger_links_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The accounts profile (REPLICAS, access control ON, the accounts + redis
    identity providers, redis hooks) PLUS the webhook-verifier lifecycle modules,
    the backup router, and raised ``trigger_*`` rate-limit knobs — the dedicated
    home of the trigger-link journey.

    Only additive over ``build_accounts_stack``: the verifier modules the verifier bind
    validates against, the backup router the round-trip legs export/import through,
    and the raised trigger family so the suite's many fires share one localhost
    bucket without tripping the default limiter. No shared builder is mutated."""
    base = build_accounts_stack(res, variants)
    manifest = {**base.manifest}
    manifest["lifecycle_modules"] = [
        *base.manifest["lifecycle_modules"],
        "tai42_webhook_verifier_github",
        "tai42_skeleton.webhooks.builtin.shared_secret",
    ]
    manifest["routers_modules"] = [*base.manifest["routers_modules"], "tai42_skeleton.routers.backup"]
    env = {**base.env}
    # The trigger family is default-ON at 60/min + 10/10s PER CLIENT IP; the whole
    # suite fires from one localhost bucket, so raise both windows well above the
    # suite's fire volume (the limiter stays ON, exactly as production runs it).
    env["TAI_RATE_LIMIT_FAMILIES__TRIGGER__LIMIT"] = "10000"
    env["TAI_RATE_LIMIT_FAMILIES__TRIGGER__BURST"] = "10000"
    return replace(base, name="trigger-links", manifest=manifest, env=env)


def _seed_trigger_links_auth(infra: Infra, resources: StackResources) -> str:
    """Seed the root ``*`` key + catch-all route table (``seed_bootstrap_key``), then
    re-scope it so the public resolver door is reachable unauthenticated: pin
    ``/trigger/*`` to the public marker and exclude it from the catch-all protected
    pattern (deny wins across tiers, so an unexcluded catch-all match would keep the
    door protected). The authed CRUD routes stay under the catch-all → ``e2e-all``."""
    raw = seed_bootstrap_key(infra, resources)
    seed_route_rows(
        resources,
        [
            ("trigger-public", "public", r"^/trigger/.*$"),
            ("e2e-all-routes", "e2e-all", r"^/(?!health$)(?!metrics$)(?!trigger/).*$"),
        ],
    )
    return raw


@pytest.fixture(scope="module")
def trigger_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The dedicated stack, seeded with the resolver-public route table
    before boot (readiness probes are denied until the table pins them public)."""
    root = tmp_path_factory.mktemp("trigger-links")
    resources, config = allocate_and_build(infra, root, build_trigger_links_stack, None, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        root_token = _seed_trigger_links_auth(infra, resources)
    except BaseException:
        stack.teardown()
        raise
    stack.auth_token = root_token
    with stack, diagnostics.track(stack):
        yield stack


@pytest.fixture
async def exec_key(trigger_stack: TaiStack) -> str:
    """A minted key's user_id the admin binds as the identity a hook/trigger-link fires
    as. The admin caller may bind any existing key, so one freshly-minted key serves."""
    admin = trigger_stack.api(port=trigger_stack.port_a)
    user_id = f"trg-exec-{secrets.token_hex(4)}"
    await admin.post(
        "/api/auth/api-keys",
        json={"user_id": user_id, "description": "trigger exec key", "scopes": ["e2e-all"]},
    )
    return user_id


@pytest.fixture
def wipe_trigger_state(trigger_stack: TaiStack) -> Callable[[], None]:
    """The suite-local SCOPED wipe: SCAN+DEL exactly the trigger-link keys,
    the per-topic hook lists, and the name→trigger map on the stack's own logical
    DB. It NEVER touches ``hooks:topic_verifiers`` (verifier bindings must survive
    the wipe for the restore-skips-verifier leg) and NEVER flushes the DB (the shared DB
    also holds the seeded admin credential the import call authenticates with)."""
    host, port = trigger_stack.infra.settings.redis_host_port
    db = trigger_stack.resources.redis_idx

    def _wipe() -> None:
        client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        try:
            keys: list[str] = []
            for pattern in ("hooks:trigger:*", "hooks:topic:*"):
                keys.extend(client.scan_iter(match=pattern, count=100))
            if client.exists("hooks:name_trigger_map"):
                keys.append("hooks:name_trigger_map")
            if keys:
                client.delete(*keys)
        finally:
            client.close()

    return _wipe
