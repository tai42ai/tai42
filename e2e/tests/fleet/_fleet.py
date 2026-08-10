"""Shared scaffolding for the fleet-convergence scenarios.

Every convergence writer mutates the deployment through ONE worker and then proves
that EVERY worker converged. Convergence is read off the REAL per-worker probe digest
(``stack.wait_workers`` — a sha256 of each process's own ``live_manifest`` + sorted
live tool names, computed in-process; never a fabricated signal): a fleet has
converged when every distinct pid reports the SAME digest AND that digest DIFFERS
from the pre-mutation baseline. The resync a mutation broadcasts may still be in
flight when the mutating call returns, so :func:`wait_converged` POLLS (bounded by a
deadline) rather than sampling once.

The stacks here are MULTIWORKER (``--workers 2`` behind ONE port), the shape whose
shared socket lets ``wait_workers`` load-balance across both uvicorn workers and see
two distinct pids — the fleet the digest convergence is asserted over. ``workers >
1`` also wires the worker bus (harness policy; see ``TaiStack._needs_bus``), so a
mutation on one worker fans its ``reload_config`` out to the sibling.

The builders reuse the ``bare`` / ``connectors`` profiles and re-shape them to that
MULTIWORKER-no-backend fleet, adding only what the scenarios need (the backup router;
for the ``!ENV`` scenarios a raw-text manifest carrying a marker the digest resolves).
"""

from __future__ import annotations

import base64
import copy
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import build_bare_stack, build_connectors_stack
from tai42_e2e.stack import StackConfig, Topology
from tai42_e2e.waiting import WaitTimeout

if TYPE_CHECKING:
    from tai42_e2e.stack import StackResources, TaiStack
    from tai42_e2e.variants import Variants

# The MULTIWORKER worker count every convergence fleet boots. Two uvicorn workers behind one
# port is the minimum that exercises cross-worker convergence while keeping the
# opt-in stack's boot cost down; ``wait_workers`` load-balances across both pids.
FLEET_WORKERS = 2


def _pin_hashseed(env: dict[str, str]) -> dict[str, str]:
    """Pin ``PYTHONHASHSEED`` across the fleet. ``uvicorn --workers N`` SPAWNS its
    workers as fresh interpreters, each with its own hash randomization; the probe
    digest hashes ``live_manifest``, which model-dumps set-typed fields (e.g.
    ``user_tools``) to lists whose order follows set iteration — so unpinned workers
    serialize the SAME persisted state into DIFFERENT digests and never converge (an
    artifact of the probe's serialization, not a real fleet divergence). A shared seed
    gives every worker one set-iteration order, so identical state yields one digest."""
    return {**env, "PYTHONHASHSEED": "0"}


def build_fleet_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The base convergence fleet: the ``bare`` profile (full core-router surface, no storage
    provider, no backend) re-shaped to MULTIWORKER(2) and given the backup router.

    ``bare`` already carries the probe tools (``e2e_worker_info`` — the digest
    source), ``tai42_toolbox.extensions.batch`` (the branch extension), and the
    manifest / tool-extensions / config routers. Two workers wire the bus."""
    cfg = build_bare_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["routers_modules"] = [*manifest["routers_modules"], "tai42_skeleton.routers.backup"]
    # No metrics sidecar: the fleet convergence proof is the probe digest, not a scrape,
    # so the extra process is pure boot/teardown cost this opt-in suite does not need.
    return _replace(
        cfg, name="fleet", manifest=manifest, env=_pin_hashseed(cfg.env), workers=FLEET_WORKERS, run_metrics=False
    )


def build_backup_source_stack(res: StackResources, variants: Variants) -> StackConfig:
    """A SINGLE-worker busless stack with the backup router — the backup export source. It
    only exports (no convergence is asserted on it), so it needs no fleet; one worker
    keeps the two-stack scenario's total process load down."""
    cfg = build_bare_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["routers_modules"] = [*manifest["routers_modules"], "tai42_skeleton.routers.backup"]
    return _replace(cfg, name="backup-source", manifest=manifest, env=_pin_hashseed(cfg.env), run_metrics=False)


def build_backup_populated_stack(res: StackResources, variants: Variants) -> StackConfig:
    """A SINGLE-worker, access-control-ON backup stack that can hold a REAL conversation
    route (its ``callback_secret``) AND a REAL api-key token — the populated source for the
    two mode-less skip-only legs pinned at e2e:

    * the ``conversations`` section, over the redis conversations backend
      (``CONVERSATIONS_REDIS_URL``), so an ``api``-door route row persists and carries a
      minted ``callback_secret``; a ``skip`` re-import must leave that secret UNCHANGED
      (never surfaced in the report's ``new_callback_secrets``);
    * the ``access_control`` section, over the seeded root key + the api-key store, so an
      existing token is left untouched (``skipped_existing``) under EVERY mode.

    It re-shapes the ``bare`` core surface (proven bootable) by MOUNTING the
    conversations / api-keys / agents routers plus the backup router, turning access
    control ON with the Postgres policy store + pluggable identity provider (seeded
    pre-boot by ``boot_stack(seed_auth=True)``), and registering ``tools_agent`` ONLY so a
    route's ``target_name`` resolves — no turn is ever run, so the scripted LLM stub is
    never dialed. One worker, no backend runtime, no metrics sidecar keeps this opt-in
    stack's boot cost down; convergence is not asserted on it."""
    # Local import so the fleet helpers stay a thin re-shape layer over the shared builders.
    from tai42_e2e.manifests import _llm_env, _memory_agent_state_env

    cfg = build_bare_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["lifecycle_modules"] = [variants.identity.lifecycle_module]
    manifest["routers_modules"] = [
        *manifest["routers_modules"],
        "tai42_skeleton.routers.conversations",
        "tai42_skeleton.routers.api_keys",
        "tai42_skeleton.routers.agents",
        "tai42_skeleton.routers.backup",
    ]
    manifest["agents"] = [
        {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]}
    ]
    env = dict(cfg.env)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    return _replace(cfg, name="backup-populated", manifest=manifest, env=env, run_metrics=False, auth=True)


def build_fleet_env_stack_builder(env_key: str) -> Callable[[StackResources, Variants], StackConfig]:
    """A builder for a convergence fleet whose seeded manifest references *env_key* via an
    ``!ENV`` marker (in a zero-transport ``mcp`` entry's ``url``), so writing or
    importing *env_key* changes the RESOLVED live view and therefore the digest.

    The marker resolves to ``pyaml_env``'s ``"N/A"`` default while *env_key* is unset
    at boot, then to the written value after the mutation. The harness ``safe_dump``
    path cannot carry a YAML tag, so this seeds the manifest verbatim (``raw_manifest``)."""

    def build(res: StackResources, variants: Variants) -> StackConfig:
        cfg = build_fleet_stack(res, variants)
        document = copy.deepcopy(cfg.manifest)
        # The marker lives on its own mcp entry so it never collides with a scenario's
        # mcp mutation; a zero-transport (unresolvable) url is skipped by the viability
        # probe but still lands, resolved, in the live view the digest hashes.
        document.pop("mcp", None)
        marker = f"mcp:\n  - title: e2e_env_probe\n    config:\n      url: !ENV ${{{env_key}}}\n"
        raw = yaml.safe_dump(document, sort_keys=False) + marker
        return _replace(cfg, name="fleet-env", raw_manifest=raw)

    return build


def build_fleet_connectors_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The convergence fleet with the connector surface: the ``connectors`` profile (connector
    router + fixture provider lifecycle module + KEK/HMAC + connector store + stub-IdP
    env) re-shaped to MULTIWORKER(2), no backend WORKER process.

    ``backend_module`` stays in the manifest — the profile's probe tools carry
    ``sync_task`` branches whose extension only registers on the backend module's import,
    so dropping it aborts startup validation. It registers only the client side on the
    serve workers (no worker process runs), and ``workers > 1`` wires the bus the
    registered backend requires. The connector engine runs in-process on the serve
    workers, so a connect converges the serve fleet with no backend runtime."""
    cfg = build_connectors_stack(res, variants)
    return _replace(
        cfg,
        name="fleet-connectors",
        env=_pin_hashseed(cfg.env),
        topology=Topology.MULTIWORKER,
        workers=FLEET_WORKERS,
        run_backend=False,
        run_metrics=False,
    )


def connectors_resource_kwargs(idp_base_url: str) -> dict[str, str]:
    """The ``resource_kwargs`` the connector fleet needs — the stub-IdP origin plus
    random per-stack connector crypto keys (padded standard base64, as the connector
    settings' strict decode requires; mirrors the root ``connectors_stack`` fixture)."""
    return {
        "idp_base_url": idp_base_url,
        "connectors_kek": base64.b64encode(secrets.token_bytes(32)).decode(),
        "connectors_state_hmac_key": base64.b64encode(secrets.token_bytes(32)).decode(),
    }


def manifest_file(stack: TaiStack) -> Path:
    """The stack's persisted ``manifest.yml`` on disk — the file the out-of-band edit
    writes past the pipeline, and the byte-identity operand for the corrupted import."""
    return stack.root / "config" / "manifest.yml"


async def converged_digest(stack: TaiStack, *, deadline: float = 60.0, differ_from: str | None = None) -> str:
    """Poll the per-worker probe until every worker reports ONE identical digest,
    returning it. With *differ_from* set, the poll additionally requires the converged
    digest to differ from that pre-mutation baseline — so this both waits out an
    in-flight resync AND asserts the mutation actually moved the fleet.

    Each attempt re-samples every worker via a fresh ``wait_workers`` call (uvicorn
    load-balances the shared port), so a worker that answered pre-reload on one attempt
    is re-read post-reload on the next; convergence is never inferred from one sample.
    A ``wait_workers`` timeout inside an attempt — a worker still holding its reload
    gate, so the shared port has not yet surfaced both pids — is a transient "not
    converged yet", polled past on the outer deadline, not a hard failure."""

    last_seen: dict[int, str] = {}

    async def attempt() -> str | None:
        try:
            seen = await stack.wait_workers(FLEET_WORKERS, deadline=10.0)
        except WaitTimeout:
            return None
        last_seen.clear()
        last_seen.update(seen)
        digests = set(seen.values())
        if len(digests) != 1:
            return None
        (digest,) = tuple(digests)
        if differ_from is not None and digest == differ_from:
            return None
        return digest

    goal = "converge" if differ_from is None else "converge away from the pre-mutation baseline"

    try:
        return await wait_for_async(attempt, deadline=deadline, message="")
    except WaitTimeout as exc:
        raise WaitTimeout(
            f"the {FLEET_WORKERS}-worker fleet never reached: {goal} "
            f"(after {deadline:.1f}s; last per-pid digests: "
            f"{ {pid: d[:12] for pid, d in last_seen.items()} }; "
            f"differ_from={differ_from[:12] if differ_from else None})"
        ) from exc


async def converged_baseline(stack: TaiStack, *, deadline: float = 60.0) -> str:
    """The pre-mutation baseline digest, captured from a KNOWN-converged fleet.

    Every worker is first driven to reload from persisted state (``POST
    /api/fleet/reload-config``), which puts both onto the identical persisted view
    deterministically, then the converged digest is read. This normalizes away a
    boot-time self-resync that, under the opt-in suite's heavy sequential load, can
    briefly leave one worker off the persisted view — a divergence any reload heals and
    which would otherwise make ``differ_from=<baseline>`` ill-defined. The scenario's
    OWN mutation still converges the fleet on its own broadcast; this only fixes the
    starting point the mutation is measured against."""
    await stack.api().post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    return await converged_digest(stack, deadline=deadline)


def assert_fleet_fanout(response: dict[str, Any]) -> dict[str, Any]:
    """Assert a mutation response embeds a reachable multi-worker fleet fan-out report
    (``mode == "fleet"``) and return it — the mode-wrapped ApplyResult shape every
    pipeline writer (config/tools/connector mutation) carries under ``fanout``."""
    fanout = response.get("fanout")
    assert isinstance(fanout, dict), f"the mutation response carried no fanout report: {response}"
    assert fanout["mode"] == "fleet", f"the mutation did not fan out over the fleet: {fanout}"
    return fanout


async def wait_present(check: Callable[[], Awaitable[bool]], *, deadline: float = 10.0, message: str) -> None:
    """Poll an async predicate until true (a thin, typed wrapper over the sanctioned
    async waiter) — used for the tool-listing side-observables that ride alongside the
    digest proof (a bound managed tool appearing / disappearing on the served port)."""
    await wait_for_async(lambda: check(), deadline=deadline, message=message)


def _replace(cfg: StackConfig, **changes: Any) -> StackConfig:
    """``dataclasses.replace`` for a ``StackConfig`` (kept local so the builders read
    as data re-shapes, not dataclass plumbing)."""
    import dataclasses

    return dataclasses.replace(cfg, **changes)
