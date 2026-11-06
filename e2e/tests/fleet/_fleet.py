"""Shared scaffolding for the fleet-convergence scenarios.

Every convergence writer mutates the deployment through ONE worker and then proves that
EVERY worker applied it. The proof is the mutation's own fan-out report, the per-worker
outcome the platform returns in the response: the worker that served the request reports
its local apply, every other targeted worker its reply over the worker bus. A fleet
mutation converged when every serve member on the bus census at the time of the call has
an ``applied`` entry and no entry is anything else (:func:`assert_applied_by`); a targeted
op when exactly its targets applied and no other worker has an entry
(:func:`assert_applied_by_exactly`).

That a call delivered its op to NO given worker is read off that worker's own presence row
after the call: a worker stamps ``last_op`` (the op's name and the UTC instant ``at``) on its
row for every op delivered to it over the bus, so a row whose generation is the one read
before the call and whose ``last_op`` does not name the call's op at or after the call's
start took no delivery of it (:func:`assert_no_op_delivered`). Ops the platform delivers on
its own schedule (a boot join's announcement, a reconnect, a roll) name other ops and do
not count.

The served-surface reads beside the report (a tool listed, a manifest title present) prove
what a caller reads after the change from the one worker that served that read.

The stacks here are MULTIWORKER (``--workers 2`` behind ONE port). ``workers > 1`` wires the
worker bus (harness policy; see ``TaiStack._needs_bus``), so a mutation on one worker fans
its ``reload_config`` out to the sibling.

The builders reuse the ``bare`` / ``connectors`` profiles and re-shape them to that
MULTIWORKER-no-backend fleet, adding only what the scenarios need (the backup router; for
the ``!ENV`` scenarios a raw-text manifest carrying a marker the written env key resolves).
"""

from __future__ import annotations

import base64
import copy
import secrets
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from tai42_e2e import StackConfig, Topology, wait_for, wait_for_async
from tai42_e2e.manifests import build_bare_stack, build_connectors_stack

if TYPE_CHECKING:
    from tai42_e2e import StackResources
    from tai42_e2e.stack import TaiStack
    from tai42_e2e.variants import BusWorker, Variants

# The MULTIWORKER worker count every convergence fleet boots. Two uvicorn workers behind one
# port is the minimum that exercises cross-worker convergence while keeping the
# opt-in stack's boot cost down.
FLEET_WORKERS = 2


def build_fleet_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The base convergence fleet: the ``bare`` profile (full core-router surface, no storage
    provider, no backend) re-shaped to MULTIWORKER(2) and given the backup router.

    ``bare`` already carries the probe tools, ``tai42_toolbox.extensions.batch`` (the branch
    extension), and the manifest / tool-extensions / config routers. Two workers wire the bus."""
    cfg = build_bare_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["routers_modules"] = [*manifest["routers_modules"], "tai42_skeleton.routers.backup"]
    # No metrics sidecar: the fleet convergence proof is the fan-out report, not a scrape,
    # so the extra process is pure boot/teardown cost this opt-in suite does not need.
    return _replace(cfg, name="fleet", manifest=manifest, workers=FLEET_WORKERS, run_metrics=False)


def build_backup_source_stack(res: StackResources, variants: Variants) -> StackConfig:
    """A SINGLE-worker busless stack with the backup router — the backup export source. It
    only exports (no convergence is asserted on it), so it needs no fleet; one worker
    keeps the two-stack scenario's total process load down."""
    cfg = build_bare_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["routers_modules"] = [*manifest["routers_modules"], "tai42_skeleton.routers.backup"]
    return _replace(cfg, name="backup-source", manifest=manifest, run_metrics=False)


def build_backup_storage_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The single-worker backup source plus the selected storage provider: a stack whose
    content store the ``templates`` section exports and restores."""
    cfg = build_backup_source_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["storage_module"] = variants.storage.module
    return _replace(cfg, name="backup-storage", manifest=manifest)


def build_backup_definitions_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The single-worker backup source plus the redis conversations backend: a stack that holds
    every stored definition a binding rides on — a hook, a preset, a per-target conversation
    config — beside the states store, so a backup of them restores onto a second, fresh stack."""
    cfg = build_backup_source_stack(res, variants)
    manifest = copy.deepcopy(cfg.manifest)
    manifest["routers_modules"] = [*manifest["routers_modules"], "tai42_skeleton.routers.conversations"]
    env = dict(cfg.env)
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    return _replace(cfg, name="backup-definitions", manifest=manifest, env=env)


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
    importing *env_key* changes the RESOLVED config the write validates and every worker
    reloads.

    The marker resolves to ``pyaml_env``'s ``"N/A"`` default while *env_key* is unset
    at boot, then to the written value after the mutation. The harness ``safe_dump``
    path cannot carry a YAML tag, so this seeds the manifest verbatim (``raw_manifest``)."""

    def build(res: StackResources, variants: Variants) -> StackConfig:
        cfg = build_fleet_stack(res, variants)
        document = copy.deepcopy(cfg.manifest)
        # The marker lives on its own mcp entry so it never collides with a scenario's
        # mcp mutation; a zero-transport (unresolvable) url is skipped by the viability
        # probe but still lands, resolved, in each worker's live view.
        document.pop("mcp", None)
        marker = f"mcp:\n  - title: e2e_env_probe\n    config:\n      url: !ENV ${{{env_key}}}\n"
        raw = yaml.safe_dump(document, sort_keys=False) + marker
        return _replace(cfg, name="fleet-env", raw_manifest=raw)

    return build


def build_fleet_connectors_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The convergence fleet with the connector surface: the ``connectors`` profile (connector
    router + fixture provider descriptors on the manifest ``connectors`` field + KEK/HMAC +
    connector store + stub-IdP env) re-shaped to MULTIWORKER(2), no backend WORKER process.

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


def serve_members(stack: TaiStack) -> list[str]:
    """The names of the serve workers on the bus census now, sorted — the members a fleet
    mutation issued next must report applied."""
    return sorted(w.name for w in stack.census() if w.kind == "serve")


def serve_generations(stack: TaiStack) -> dict[str, int]:
    """The generation of every serve worker on the bus census now, by worker name — read
    before a call, the lives :func:`assert_no_op_delivered` requires unchanged after it."""
    return {w.name: w.generation for w in stack.census() if w.kind == "serve"}


def call_start() -> datetime:
    """The UTC instant a call starts, read just before issuing it: the workers on this host
    stamp ``last_op.at`` from the same clock."""
    return datetime.now(UTC)


def _entries(report: Mapping[str, Any]) -> str:
    return "; ".join(
        f"{r['name']}: {r['outcome']}" + "".join(f" ({r[k]})" for k in ("detail", "error") if r.get(k))
        for r in report.get("results") or []
    )


def assert_applied_by(report: Mapping[str, Any], members: Collection[str]) -> None:
    """Assert a fleet fan-out report (``op`` + per-worker ``results``) shows every one of
    ``members`` applied and no entry unconfirmed, naming every entry on a failure."""
    outcomes = {r["name"]: r["outcome"] for r in report["results"]}
    missing = sorted(set(members) - outcomes.keys())
    unconfirmed = sorted(name for name, outcome in outcomes.items() if outcome != "applied")
    if missing or unconfirmed:
        pytest.fail(
            f"{report['op']} did not converge on {sorted(members)}: "
            f"no entry for {missing}, not applied by {unconfirmed}; entries: {_entries(report)}"
        )


def assert_applied_by_exactly(report: Mapping[str, Any], targets: Collection[str]) -> None:
    """Assert a targeted fan-out report shows exactly ``targets`` applied and no entry for any
    other worker, naming every entry on a failure."""
    outcomes = {r["name"]: r["outcome"] for r in report["results"]}
    assert outcomes == dict.fromkeys(targets, "applied"), (
        f"{report['op']} targeted at {sorted(targets)} did not apply on exactly them; entries: {_entries(report)}"
    )


def wait_op_stamped(
    stack: TaiStack, report: Mapping[str, Any], started_at: datetime, *, deadline: float = 15.0
) -> None:
    """Wait until the presence rows carry an untargeted op's deliveries.

    ``report`` is the op's fan-out report and ``started_at`` the :func:`call_start` read
    before the op was issued. A worker stamps ``last_op`` on its row once it has replied, so
    the report can be in hand ahead of its stamps; the worker that served the request applied
    the op in-process and takes no delivery, so every applied worker but that one stamps the
    op at or after ``started_at``."""
    op = report["op"]
    applied = [r["name"] for r in report["results"] if r["outcome"] == "applied"]
    rows: dict[str, BusWorker] = {}

    def stamped() -> bool:
        rows.clear()
        rows.update((w.name, w) for w in stack.census())
        stamped_by = [name for name in applied if name in rows and _stamped_since(rows[name], op, started_at)]
        return len(stamped_by) >= len(applied) - 1

    wait_for(
        stamped,
        deadline=deadline,
        message=lambda: (
            f"the presence rows never carried {op} since {started_at.isoformat()} on every applied worker but the "
            f"one that served it: applied by {applied}; rows: {_rows_text(rows.values())}"
        ),
    )


def assert_no_op_delivered(
    stack: TaiStack, names: Collection[str], op: str, started_at: datetime, generations: Mapping[str, int]
) -> None:
    """Assert the call that started at ``started_at`` delivered ``op`` to none of ``names``.

    Each named worker's presence row, read now, keeps the generation ``generations`` holds
    for it (it did not restart) and its ``last_op`` does not name ``op`` at or after
    ``started_at``. A stamp of another op, or of ``op`` dated before the call, is not a
    delivery of this call."""
    rows = {w.name: w for w in stack.census()}
    reached = sorted(
        name
        for name in names
        if name not in rows or rows[name].generation != generations[name] or _stamped_since(rows[name], op, started_at)
    )
    assert not reached, (
        f"{op} since {started_at.isoformat()} reached {reached} (generations before the call: "
        f"{ {n: generations[n] for n in reached} }); rows now: {_rows_text(rows[n] for n in reached if n in rows)}"
    )


def _stamped_since(row: BusWorker, op: str, started_at: datetime) -> bool:
    """Whether ``row``'s ``last_op`` names ``op`` stamped at or after ``started_at``."""
    last = row.last_op
    if last is None or last["op"] != op:
        return False
    return datetime.fromisoformat(last["at"]) >= started_at


def _rows_text(rows: Iterable[BusWorker]) -> str:
    return "; ".join(f"{w.name} (pid {w.pid}, generation {w.generation}, {w.state}, last_op {w.last_op})" for w in rows)


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
    async waiter) — used for the served-surface reads that ride alongside the fan-out
    report (a bound managed tool appearing / disappearing on the served port)."""
    await wait_for_async(lambda: check(), deadline=deadline, message=message)


def _replace(cfg: StackConfig, **changes: Any) -> StackConfig:
    """``dataclasses.replace`` for a ``StackConfig`` (kept local so the builders read
    as data re-shapes, not dataclass plumbing)."""
    import dataclasses

    return dataclasses.replace(cfg, **changes)
