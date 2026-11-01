"""Restarting a stack's processes and waiting for their fresh lives.

A restart stops one process and respawns it from its saved spec; an env change or a KEK
rotation rewrites the deployment env first and restarts every serve replica and the backend.
Each wait holds until the restarted process's fresh lives are on the worker-bus census, keyed
on the slot's generation (a worker reuses its stable slot name across lives).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e import child_env, ports, readiness, spawning
from tai42_e2e.port_ownership import pids_in_session
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from tai42_e2e.procs import ProcessHandle
    from tai42_e2e.stack import TaiStack
    from tai42_e2e.variants import BusWorker


def restart(stack: TaiStack, name: str) -> None:
    """Stop and respawn one process from its saved spec (component-restart
    tests). The new process re-enters readiness for its own port kind."""
    if stack.config.supervised:
        # The respawn-on-exit supervisor would observe the terminated handle and
        # double-spawn a second replacement — a supervised stack recycles through the
        # supervisor (a graceful self-exit), never a manual restart.
        raise RuntimeError(f"restart({name!r}) is unsupported on a supervised stack; recycle via the supervisor")
    handle = stack.procs[name]
    # A restarted worker answers requests before it has claimed its bus slot, so the
    # post-restart wait keys on the restarted process's fresh census LIVES. A worker
    # reuses its stable slot name at an incremented generation, so capture the
    # pre-restart lives of its kind (``{name: generation}``) and the names this process
    # holds BEFORE the kill; the wait then holds until those lives are gone and its fresh
    # READY lives have joined, keying on the generation not a new string.
    before, replaced = _restart_baseline(stack, name, handle)
    handle.terminate()
    # The respawn reuses the same port; wait for the killed process to release
    # it before rebinding, else the new process fails to bind and exits early.
    for port in stack.ports_for(name):
        wait_for(lambda p=port: ports.is_free(p), deadline=5.0, message=f"port {port} never freed before restart")
    spec = stack.specs[name]
    spawning.spawn(stack, spec)
    wait_after_restart(stack, name, before, replaced)


def restart_with_env(stack: TaiStack, changes: Mapping[str, str | None]) -> None:
    """Apply env ``changes`` (``None`` removes a key) to every serve replica and the backend, then restart them.

    Models an operator changing a deployment setting: the keys are deployment env, so the
    change reaches each process through its process env at the restart.
    """
    for key, value in changes.items():
        if value is None:
            stack.config.env.pop(key, None)
        else:
            stack.config.env[key] = value
    for name in [n for n in stack.specs if n.startswith("serve") or n == "backend"]:
        env = stack.specs[name].env
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        restart(stack, name)


def respawn_with_process_env(stack: TaiStack, name: str, changes: Mapping[str, str | None]) -> None:
    """Apply env ``changes`` (``None`` removes a key) to one supervised process, then let the supervisor respawn it.

    Models an operator changing one process's deployment env on a supervised deployment:
    the process's spec gains the changes, the process is stopped, and the respawn-on-exit
    supervisor starts it again from that spec. Returns once the fresh life is ready.
    """
    if not stack.config.supervised:
        raise RuntimeError(f"respawn_with_process_env({name!r}) needs a supervised stack; use restart_with_env")
    with stack.supervisor.lock:
        if not stack.supervisor.watches(name):
            raise RuntimeError(f"respawn_with_process_env({name!r}): the supervisor does not watch {name!r}")
        handle = stack.procs[name]
        before, replaced = _restart_baseline(stack, name, handle)
        env = stack.specs[name].env
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        # Stopped under the supervisor lock, so the supervisor's next pass respawns it from
        # the changed spec.
        handle.terminate()
    wait_after_restart(stack, name, before, replaced)


def rotate_connectors_kek(stack: TaiStack, *, new_kek: str, previous: list[str]) -> None:
    """Rotate ``CONNECTORS_KEK`` across every serve replica and the backend worker.

    Sets the new current key and the previous-key ring in each running process's env,
    then restarts it so the new keys take effect — modelling an operator rotating the
    deployment's KEK. An empty ``previous`` clears ``CONNECTORS_KEK_PREVIOUS`` (the old
    key retired after the re-encrypt sweep converged)."""
    # The KEK is deployment env: ``config.env`` carries the rotated keys so a process the
    # harness spawns later boots with them too.
    stack.config.env["CONNECTORS_KEK"] = new_kek
    if previous:
        stack.config.env["CONNECTORS_KEK_PREVIOUS"] = ",".join(previous)
    else:
        stack.config.env.pop("CONNECTORS_KEK_PREVIOUS", None)
    for name in [n for n in stack.specs if n.startswith("serve") or n == "backend"]:
        env = stack.specs[name].env
        env["CONNECTORS_KEK"] = new_kek
        if previous:
            env["CONNECTORS_KEK_PREVIOUS"] = ",".join(previous)
        else:
            env.pop("CONNECTORS_KEK_PREVIOUS", None)
        restart(stack, name)


def wait_after_restart(
    stack: TaiStack,
    name: str,
    before: Mapping[str, int] | None = None,
    replaced: Collection[str] | None = None,
) -> None:
    """Wait until the restarted process ``name`` serves again: its health route (and, on a bus
    stack, its fresh census lives and its drained boot gate), the metrics route, or the
    backend's fresh census life."""
    deadline = stack.infra.settings.boot_timeout
    if name.startswith("serve") or name == "embed":
        idx = 0 if name in ("serve", "serve-a", "embed") else 1
        port = stack.app_ports[idx]
        readiness.wait_http_ok(stack, f"{stack.base_url(port)}/health", deadline, "app health")
        if child_env.needs_bus(stack.config):
            # The respawned process answers /health and MCP requests before its workers
            # have claimed their bus slots; hold until every fresh worker is on the census,
            # then drain its boot self-resync gate (where the profile carries the probe) so
            # a test acting right after the restart races neither.
            count = 1 if name == "embed" else readiness.serve_workers_on_port(stack)
            stack.wait_fresh_lives("serve", deadline, before=before, replaced=replaced, count=count)
            readiness.run_readiness_coro(readiness.drain_gate_coro(stack, [port], deadline))
    elif name == "metrics":
        assert stack.metrics_port is not None
        readiness.wait_http_ok(stack, f"http://{stack.host}:{stack.metrics_port}/metrics", deadline, "metrics")
    elif name == "backend":
        stack.wait_fresh_lives("backend", deadline, before=before, replaced=replaced)


def wait_generation_bump(
    stack: TaiStack, kind_or_name: str, baseline: int | Mapping[str, int], *, deadline: float = 90.0
) -> dict[str, BusWorker]:
    """Wait until a fresh READY life supersedes the baseline generation(s), returning
    the matching census rows keyed by slot name.

    A worker's slot name is STABLE across lives; a new life bumps its generation. Two
    forms distinguished by ``baseline``:

    * Single-life (``baseline`` an ``int``): ``kind_or_name`` is a slot NAME. Waits
      until that name shows a READY row at a generation greater than ``baseline`` — the
      scenario-scoped deterministic wait for one named slot's next life.
    * Fleet (``baseline`` a ``{name: generation}`` map): ``kind_or_name`` is a worker
      KIND. Waits until EVERY baseline name shows a READY row of that kind at a
      generation greater than its baseline — proves a recycle/respawn rolled every
      targeted life without keying on a fresh unrelated id."""
    if isinstance(baseline, int):
        name, base_gen = kind_or_name, baseline

        def one_probe() -> bool:
            early = readiness.early_exit_detail(stack)
            if early is not None:
                raise RuntimeError(f"generation-bump wait: {early}")
            row = next((w for w in stack.census() if w.name == name), None)
            return row is not None and row.state == "ready" and row.generation > base_gen

        wait_for(
            one_probe, deadline=deadline, message=f"{name!r} never reached a ready life past generation {base_gen}"
        )
        row = next(w for w in stack.census() if w.name == name)
        return {name: row}

    kind, targets = kind_or_name, dict(baseline)

    def fleet_probe() -> bool:
        early = readiness.early_exit_detail(stack)
        if early is not None:
            raise RuntimeError(f"generation-bump wait: {early}")
        rows = {w.name: w for w in stack.census() if w.kind == kind}
        return all(
            name in rows and rows[name].state == "ready" and rows[name].generation > gen
            for name, gen in targets.items()
        )

    wait_for(
        fleet_probe,
        deadline=deadline,
        message=f"not every {kind!r} life bumped past its baseline generation ({targets})",
    )
    rows = {w.name: w for w in stack.census() if w.kind == kind}
    return {name: rows[name] for name in targets if name in rows}


def _restart_census_kind(stack: TaiStack, name: str) -> str | None:
    """The bus-census kind a restarted process rejoins as, or ``None`` when it joins no
    bus (the metrics server, or a busless stack's serve/embed process)."""
    if name == "backend":
        return "backend"
    if (name.startswith("serve") or name == "embed") and child_env.needs_bus(stack.config):
        return "serve"
    return None


def _restart_baseline(stack: TaiStack, name: str, handle: ProcessHandle) -> tuple[dict[str, int], set[str]]:
    """The pre-restart ``{name: generation}`` of the restarted process's census kind and
    the slot names the process itself holds. Every backend-kind row counts as the
    restarted backend worker's, which holds only on a stack with no queue workers; a
    serve/embed process holds the serve lives whose pid belongs to its session
    (the spawned master is a session leader and its uvicorn workers inherit the session),
    so another replica's worker is never taken for one of its lives."""
    kind = _restart_census_kind(stack, name)
    if kind is None:
        return {}, set()
    rows = [w for w in stack.census() if w.kind == kind]
    before = {w.name: w.generation for w in rows}
    if kind == "backend":
        return before, set(before)
    return before, {w.name for w in rows if pids_in_session([w.pid], handle.pid)}
