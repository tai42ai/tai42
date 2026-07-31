"""Concurrency, persistence, and outage on the multi-worker no-backend fleet.

The mission's promises under contention and failure: two writers racing the same
manifest both land, ruamel comments survive a UI-path edit, and a bus outage (dead
redis or a silent worker) is surfaced honestly and healed.

* Two connector connects fired SIMULTANEOUSLY (``e2e_noauth_alpha`` +
  ``e2e_noauth_beta``) — both are read-modify-write APPENDS to the ``mcp`` list,
  so a lawful order can erase NEITHER — both survive in the persisted manifest
  (the lost-update regression). A writer 503'd by the reload gate retries with
  backoff (``retry_on_reloading``); the assertion is on the FINAL state.
* A ruamel-normalized COMMENTED manifest, seeded byte-verbatim, survives a UI-path
  config edit: the unedited lines are byte-identical afterward (the guarantee is
  "byte-stable modulo edited keys", not whole-file byte equality).
* Bus redis down (the BUS relay severed, feature stores untouched): a feature call
  still succeeds and every presence key EXPIRES within the SHORT heartbeat TTL. A
  separate test asserts that a config mutation during the outage returns a 200
  carrying the honest bus-unreachable fanout (connection error, no origin list),
  persists and local-reloads, and — once the relay is restored — re-registers every
  origin and reconverges the fleet without a restart.
* A SILENT worker (one REPLICAS master SIGSTOPped, its presence key kept alive by
  a long TTL): a mutation's report NAMES that origin ``missing`` (not ``departed``);
  after SIGCONT the fleet converges to the final persisted state.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import yaml
from _fleet import (
    FLEET_WORKERS,
    assert_fleet_fanout,
    build_fleet_connectors_stack,
    build_fleet_stack,
    connectors_resource_kwargs,
    converged_baseline,
    converged_digest,
    manifest_file,
)

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import build_bare_stack
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack, Topology
from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready
from tai42_e2e.variants import bus_census

if TYPE_CHECKING:
    from tai42_e2e.netfixtures import OAuthIdp
    from tai42_e2e.variants import Variants

pytestmark = pytest.mark.backendless

# A zero-transport (unreachable loopback) mcp entry — accepted by the writers, skipped
# by the viability probe, so no subprocess spawns, but it still lands, resolved, in the
# persisted manifest the assertions read.
_UNREACHABLE = {"url": "http://127.0.0.1:1/mcp"}


# ---- two concurrent writers both persist ---------------------------------


async def test_concurrent_connects_both_persist(
    fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    """Two connector connects fired at once each APPEND a manifest entry; both
    survive in the persisted document. Two DISTINCT no-auth providers so the pair is
    two independent appends (an mcp-config section REPLACE racing an append could
    lawfully overwrite; two appends cannot). Each retries past the reload gate's 503,
    and the assertion is the FINAL persisted state, not first-attempt success."""
    stack = fresh_stack(build_fleet_connectors_stack, resource_kwargs=connectors_resource_kwargs(oauth_idp.base_url))
    api = stack.api()
    await converged_baseline(stack)

    async def connect(provider_id: str, alias: str) -> dict:
        return await api.post(
            "/api/connectors/connections/start",
            json={"provider_id": provider_id, "alias": alias, "enabled_sub_services": ["default"]},
            retry_on_reloading=True,
        )

    # Fire both writers simultaneously — the race the lost-update regression lives
    # in (a read-modify-write pair with no serialization would drop one append).
    alpha, beta = await asyncio.gather(
        connect("e2e_noauth_alpha", uniq("d1_alpha")),
        connect("e2e_noauth_beta", uniq("d1_beta")),
    )
    alpha_entry = alpha["added_manifest_entries"][0]
    beta_entry = beta["added_manifest_entries"][0]

    document = yaml.safe_load(manifest_file(stack).read_text()) or {}
    titles = [entry["title"] for entry in document.get("mcp", [])]
    assert alpha_entry in titles, f"the alpha append was lost by the concurrent writer: {titles}"
    assert beta_entry in titles, f"the beta append was lost by the concurrent writer: {titles}"


# ---- comment preservation ------------------------------------------------

# A ruamel-normalized COMMENTED manifest, round-trip byte-stable (an mcp-section edit
# rewrites only the ``mcp`` block, every other line byte-identical). Seeded verbatim via
# ``raw_manifest`` because ``yaml.safe_dump`` cannot carry comments; the comments live on
# NON-mcp keys the mcp-section replace never touches.
_D2_SEED = """\
# Fleet manifest — managed by the platform team, edit with care.
routers_modules:
  - tai42_skeleton.routers.health
  - tai42_skeleton.routers.manifest
  - tai42_skeleton.routers.config
# Opt out of the default router set: mount exactly the three routers above.
default_routers: none
# The task backend is intentionally disabled in this deployment.
backend_module: ""  # no worker runtime here
mcp:
  - title: seed_server  # the originally seeded server
    config:
      url: http://127.0.0.1:1/mcp
"""


def build_d2_stack(res: StackResources, variants: Variants) -> StackConfig:
    """A single busless worker (file mode, no backend, no bus → boots and serves) whose
    manifest is the commented seed above, seeded byte-verbatim. The bare profile's env
    (feature Redis wiring) rides along; the seed replaces only the manifest document."""
    cfg = build_bare_stack(res, variants)
    return replace(cfg, name="d2-comments", raw_manifest=_D2_SEED, run_backend=False, run_metrics=False)


async def test_comment_preservation(fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]) -> None:
    """A UI-path config edit (``POST /api/mcp-config`` — the comment-preserving
    mutate seam) replaces the mcp section; every UNEDITED line stays byte-identical.

    The assertion is scoped to the unedited region (everything before the ``mcp:``
    key), per the "byte-stable modulo edited keys" guarantee — the comments and
    their exact bytes survive, while the mcp block is legitimately rewritten."""
    stack = fresh_stack(build_d2_stack)
    path = manifest_file(stack)
    assert path.read_text() == _D2_SEED, "the raw-text seed was not written byte-verbatim"

    new_title = uniq("d2_mcp")
    await stack.api().post(
        "/api/mcp-config",
        json={"mcp": [{"title": new_title, "config": _UNREACHABLE}]},
        retry_on_reloading=True,
    )

    persisted = path.read_text()
    unedited, _, _ = _D2_SEED.partition("mcp:")
    assert persisted.startswith(unedited), (
        "an mcp-section edit rewrote unedited lines (comments must be byte-stable modulo the edited keys):\n"
        f"---seed head---\n{unedited}\n---persisted---\n{persisted}"
    )
    # The unedited comments survived verbatim, and the edit itself landed.
    assert "# Fleet manifest — managed by the platform team, edit with care." in persisted
    assert 'backend_module: ""  # no worker runtime here' in persisted
    assert new_title in persisted, "the mcp-config edit did not persist"
    assert "seed_server" not in persisted, "the replaced mcp section still carries the old entry"


# ---- bus redis down ------------------------------------------------------

# A SHORT heartbeat TTL: a subscriber refreshes at a third of it, so a bus the SUT
# cannot reach lets every presence key lapse within one TTL — held past, the recovery
# re-registration is a real signal (with surviving keys the recovery poll is vacuous).
_D3A_TTL = "3"


@pytest.fixture
def relayed_bus_fleet(infra: Infra, fresh_stack: Callable[..., TaiStack]) -> tuple[TaiStack, TcpRelay]:
    """A MULTIWORKER(2) no-backend fleet whose WORKER BUS is reached through its OWN
    TcpRelay (separate from the feature-Redis endpoint), so severing it drops the bus
    without dropping auth/feature stores. The stack adopts the relay for teardown +
    leak-check; the heartbeat TTL is pinned short."""
    redis_host, redis_port = infra.settings.redis_host_port
    bus_relay = TcpRelay(redis_host, redis_port)
    try:
        bus_relay.start()
        wait_relay_ready(bus_relay)
    except BaseException:
        bus_relay.stop()
        raise
    stack = fresh_stack(
        build_fleet_stack,
        env_overrides={"TAI_BUS_HEARTBEAT_TTL": _D3A_TTL},
        resource_kwargs={"bus_redis_host": bus_relay.listen_host, "bus_redis_port": bus_relay.port},
        relays=[bus_relay],
    )
    return stack, bus_relay


def _real_bus_url(infra: Infra, stack: TaiStack) -> str:
    """The bus Redis reached DIRECTLY (bypassing the severable relay), so a test can
    observe presence-key expiry while the SUT's own bus endpoint is severed. Same
    logical DB the stack's bus uses; the harness never routes this read through the
    relay."""
    host, port = infra.settings.redis_host_port
    return f"redis://{host}:{port}/{stack.resources.redis_idx}"


async def test_bus_outage_isolates_feature_stores_and_expires_presence(
    relayed_bus_fleet: tuple[TaiStack, TcpRelay], infra: Infra
) -> None:
    """Severing the BUS relay must NOT touch the feature stores — a feature-Redis-backed
    call still succeeds — and, held past the SHORT heartbeat TTL, every presence key
    EXPIRES on the bus Redis (so the recovery re-registration a healthy bus would show is
    a real signal, not a surviving-key vacuous pass).

    The mutation-during-outage RESPONSE shape AND the restore-recovery convergence are
    asserted in :func:`test_mutation_during_outage_reports_and_fleet_recovers`."""
    stack, bus_relay = relayed_bus_fleet
    api = stack.api()

    await converged_baseline(stack)
    pre_origins = {origin.origin for origin in stack.census()}
    assert len(pre_origins) >= FLEET_WORKERS, f"the fleet did not fully register before the outage: {pre_origins}"

    # Sever the BUS only — the feature Redis/PG endpoints are not relayed.
    bus_relay.sever()

    # Feature stores are untouched by the bus outage — a feature-Redis-backed call succeeds.
    await api.post("/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}, expect=202)

    # The outage outlasts the TTL: every presence key expires on the (directly-read) bus
    # Redis — with the SUT's own bus endpoint severed, no worker can refresh its key.
    real_bus, namespace = _real_bus_url(infra, stack), stack.resources.bus_namespace

    async def keys_expired() -> bool:
        return not bus_census(real_bus, namespace)

    await wait_for_async(keys_expired, deadline=20.0, message="bus presence keys never expired during the outage")


async def test_mutation_during_outage_reports_and_fleet_recovers(
    relayed_bus_fleet: tuple[TaiStack, TcpRelay], uniq: Callable[[str], str]
) -> None:
    """With the bus severed a config mutation PERSISTS + local-reloads and returns a 200
    whose fanout is the honest bus-unreachable failure (connection error, NO origin
    list); restoring the relay re-registers every origin and reconverges the fleet
    without a restart, with a fleet reload as belt-and-braces."""
    stack, bus_relay = relayed_bus_fleet
    api = stack.api()

    baseline = await converged_baseline(stack)
    pre_origins = {origin.origin for origin in stack.census()}
    bus_relay.sever()

    # A 200 (so the local persist + reload committed — a failed local reload would raise)
    # whose fanout is the honest unreachable shape (connection error, no origin list).
    title = uniq("d3a_mcp")
    result = await api.post(
        "/api/mcp-config", json={"mcp": [{"title": title, "config": _UNREACHABLE}]}, retry_on_reloading=True
    )
    fanout = result.get("fanout")
    assert isinstance(fanout, dict), f"the mutation carried no fanout report: {result}"
    assert fanout["mode"] == "unreachable", f"a dead bus must report unreachable, not {fanout!r}"
    assert fanout["reachable"] is False, fanout
    assert fanout["results"] == [], f"an unreachable bus must name NO origins: {fanout}"
    assert fanout["error"], f"the unreachable report must carry the connection error: {fanout}"

    # It persisted despite the dead bus.
    document = yaml.safe_load(manifest_file(stack).read_text()) or {}
    titles = [entry["title"] for entry in document.get("mcp", [])]
    assert title in titles, f"the mutation did not persist during the outage: {titles}"

    # Restore the bus — recovery WITHOUT a restart. Registration PRECEDES the self-resync,
    # so re-registration is a precondition; then the digests reconverge onto the new state.
    bus_relay.restore()
    wait_relay_ready(bus_relay)

    async def reregistered() -> bool:
        workers = (await api.get("/api/fleet/workers", retry_on_reloading=True))["workers"]
        return {worker["origin"] for worker in workers} >= pre_origins

    await wait_for_async(reregistered, deadline=30.0, message="the fleet never re-registered after bus restore")
    await converged_digest(stack, differ_from=baseline)
    # Belt-and-braces recovery path.
    await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)


# ---- silent worker (SIGSTOP) ---------------------------------------------

# The two-sided silent-worker envelope: ack+apply deadlines SHORT (the report cuts to the
# ``missing`` verdict quickly and the mutation is bounded), heartbeat TTL well ABOVE the
# scenario window (the stopped worker's presence key survives, so the verdict is ``missing``
# — alive but silent — never ``departed``).
_D3B_ACK_TIMEOUT = "2"
_D3B_APPLY_TIMEOUT = "3"
_D3B_TTL = "60"


def build_d3b_stack(res: StackResources, variants: Variants) -> StackConfig:
    """A REPLICAS(2) no-backend fleet — two ``--workers 1`` masters, so a SIGSTOPped
    replica has NO uvicorn multiprocess supervisor to SIGKILL it (a ``-w N`` child
    would be reaped in ~5s). ``PYTHONHASHSEED`` pinned so the two replicas' probe
    digests are comparable; the ack/apply deadlines and TTL pin the silent-worker
    envelope."""
    cfg = build_bare_stack(res, variants)
    env = {
        **cfg.env,
        "PYTHONHASHSEED": "0",
        "TAI_BUS_ACK_TIMEOUT": _D3B_ACK_TIMEOUT,
        "TAI_BUS_APPLY_TIMEOUT": _D3B_APPLY_TIMEOUT,
        "TAI_BUS_HEARTBEAT_TTL": _D3B_TTL,
    }
    return replace(cfg, name="d3b-replicas", topology=Topology.REPLICAS, run_backend=False, run_metrics=False, env=env)


async def _probe(stack: TaiStack, port: int) -> tuple[int, str]:
    """The ``(pid, state_digest)`` a specific replica reports — REPLICAS gives each
    replica its own port, so the probe addresses one worker deterministically (unlike
    the load-balanced MULTIWORKER port the ``_fleet`` helpers sample)."""
    async with stack.mcp(port=port) as mcp:
        result = await mcp.call_tool("e2e_worker_info", retry_on_reloading=True)
    data = result.data if result.data is not None else result.structured_content
    if not isinstance(data, dict) or "pid" not in data or "state_digest" not in data:
        raise RuntimeError(f"e2e_worker_info returned an unexpected shape: {data!r}")
    return int(data["pid"]), str(data["state_digest"])


async def _replicas_converged(stack: TaiStack, *, deadline: float = 60.0, differ_from: str | None = None) -> str:
    """Poll both replica ports until they report ONE identical digest, returning it.
    With *differ_from*, additionally require it to differ from that baseline — so this
    both waits out an in-flight resync AND asserts the fleet actually moved."""

    async def attempt() -> str | None:
        _, digest_a = await _probe(stack, stack.port_a)
        _, digest_b = await _probe(stack, stack.port_b)
        if digest_a != digest_b:
            return None
        if differ_from is not None and digest_a == differ_from:
            return None
        return digest_a

    goal = "converge" if differ_from is None else "converge away from the baseline"
    return await wait_for_async(attempt, deadline=deadline, message=f"the two replicas never reached: {goal}")


async def test_silent_worker_reports_missing_then_converges(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """One REPLICAS master SIGSTOPped goes silent while its presence key survives
    (long TTL). A mutation on the LIVE replica names the silent origin ``missing`` in
    its publish-time report — alive but silent, not ``departed``. After SIGCONT the
    fleet converges to the final persisted state (a fleet reload converges it; the
    buffered broadcast may also apply on resume — the assertion is the final digest)."""
    stack = fresh_stack(build_d3b_stack)
    api_a = stack.api(port=stack.port_a)

    # Normalize both replicas onto one baseline digest.
    await api_a.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    baseline = await _replicas_converged(stack)

    # Map replica B's bus origin via the pid it serves on port_b (a single-process
    # ``--workers 1`` master answers the probe from the same pid the bus presence carries).
    b_pid, _ = await _probe(stack, stack.port_b)

    async def both_serve_origins() -> list | None:
        serve = [origin for origin in stack.census() if origin.kind == "serve"]
        return serve if len(serve) >= 2 else None

    origins = await wait_for_async(
        both_serve_origins, deadline=20.0, message="the two serve replicas never both registered"
    )
    b_origin = next((origin.origin for origin in origins if origin.pid == b_pid), None)
    assert b_origin is not None, f"no bus origin matched replica B pid {b_pid}: {origins}"

    b_pgid = os.getpgid(stack.process("serve-b").pid)
    os.killpg(b_pgid, signal.SIGSTOP)
    try:
        # A mutation on the live replica: replica B is in the census (its key survives),
        # so it is an expected origin — silent, it is cut to ``missing`` at the apply
        # deadline (not ``departed``, which would need an expired key).
        title = uniq("d3b_mcp")
        result = await api_a.post(
            "/api/mcp-config", json={"mcp": [{"title": title, "config": _UNREACHABLE}]}, retry_on_reloading=True
        )
        fanout = assert_fleet_fanout(result)
        outcomes = {entry["origin"]: entry["outcome"] for entry in fanout["results"]}
        assert outcomes.get(b_origin) == "missing", f"the SIGSTOPped replica was not reported missing: {fanout}"
    finally:
        # Always resume replica B — a stopped process left behind would fail teardown.
        os.killpg(b_pgid, signal.SIGCONT)

    # Final convergence: a fleet reload converges the resumed replica onto the persisted
    # state, so both replicas land on ONE new digest carrying the mutation.
    await api_a.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    await _replicas_converged(stack, differ_from=baseline)
    document = yaml.safe_load(manifest_file(stack).read_text()) or {}
    assert title in [entry["title"] for entry in document.get("mcp", [])], "the mutation did not persist"
