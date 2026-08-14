"""Mechanical certification of a backend's declarations.

The bar a vendor binding has to clear is "declare a runtime and implement it";
everything else is the host's. That bar is only real if it is executable, so the
rules live here as data rather than in a review checklist: a plugin adds a
five-line test calling :func:`check_backend_declarations` and gets the same
verdict the launch path enforces.

pytest-free and dependency-free, so it ships in the wheel and any test runner
can call it.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from tai42_contract.backend.runtime import CONSUMING_RUNTIME, BackendRuntime, ExecutionMode

# Signal installs a runtime may never make: the host owns the process's signal
# disposition, ``add_signal_handler`` is last-writer-wins, and ``signal.signal``
# is a C-level rebind that removes asyncio's signal machinery outright.
_FORBIDDEN_SIGNAL_CALLS = re.compile(r"add_signal_handler|signal\s*\.\s*signal\s*\(")

# The one wording for the overridden-template verdict, so the launch path and a
# conformance test cannot report the same defect two different ways.
OVERRIDDEN_LAUNCH_VERDICT = (
    "{backend} overrides launch: the template is what makes readiness gating, shutdown wiring "
    "and drain-on-cancellation identical across backends, so it is never re-implemented"
)

# Sentinel for a declaration the binding never made. ``None`` would be
# ambiguous — a runtime could plausibly declare it.
_UNDECLARED = object()


def _declared(obj: object, attr: str) -> Any:
    return getattr(obj, attr, _UNDECLARED)


def check_runtime_declarations(runtime: BackendRuntime | type[BackendRuntime]) -> list[str]:
    """Every way ``runtime``'s declarations and its bindings disagree.

    Accepts a class or an instance: ``pool_turnover_required`` may be decided per
    instance by a launch option, so the launch path checks the live object while
    a conformance test checks the declared class.

    A MISSING declaration is a reported problem, never a raise: the whole point
    is to hand a binding the full list of what it still owes, and an
    ``AttributeError`` on the first gap hides the rest.
    """
    cls = runtime if isinstance(runtime, type) else type(runtime)
    name = cls.__name__
    problems: list[str] = []

    if _declared(runtime, "name") is _UNDECLARED:
        problems.append(f"{name} declares no name, so no launch subcommand can select it")

    mode = _declared(runtime, "mode")
    if mode is _UNDECLARED:
        problems.append(f"{name} declares no mode, so the host cannot tell how to drive its run body")
    elif not isinstance(mode, ExecutionMode):
        # ``ExecutionMode`` is a ``StrEnum``, so a plain ``"on_loop"`` compares EQUAL
        # to the member while failing every ``is`` check the host drives the body
        # with — the runtime would be silently treated as none of the three modes.
        problems.append(
            f"{name} declares mode={mode!r}, which is not an ExecutionMode member; the host dispatches on "
            "identity, so a look-alike string matches no mode at all"
        )
    else:
        if mode is ExecutionMode.on_loop and not _overrides(cls, "run_on_loop"):
            problems.append(f"{name} declares mode={mode.value} but implements no run_on_loop")
        if mode in (ExecutionMode.worker_thread, ExecutionMode.inline) and not _overrides(cls, "run_blocking"):
            problems.append(f"{name} declares mode={mode.value} but implements no run_blocking")

    if runtime.consumes_work:
        if not _overrides(cls, "request_drain"):
            problems.append(f"{name} declares consumes_work but implements no request_drain")
        if mode is ExecutionMode.inline:
            problems.append(
                f"{name} declares consumes_work with mode={mode.value}: a runtime that blocks the serving loop "
                "cannot be drained from it, so it may not consume work"
            )

    if runtime.pool_turnover_required:
        if not _overrides(cls, "turn_over_pool"):
            problems.append(f"{name} declares pool_turnover_required but implements no turn_over_pool")
        if not runtime.consumes_work:
            problems.append(
                f"{name} declares pool_turnover_required without consumes_work: a runtime that pulls no work "
                "holds no pool to turn over"
            )

    return problems


def check_backend_declarations(backend: object) -> list[str]:
    """Every way ``backend`` and its runtimes fail the shared-base contract.

    An empty list is the certification: this backend gets readiness gating,
    composed shutdown wiring, drain-on-cancellation and (where declared) pool
    turnover from the base, with no host-shaped code of its own.
    """
    from tai42_kit.backend.base import ManagedBackend

    if not isinstance(backend, ManagedBackend):
        return [f"{type(backend).__name__} is not a ManagedBackend, so the shared launch lifecycle does not drive it"]

    cls = type(backend)
    problems: list[str] = []
    if cls.launch is not ManagedBackend.launch:
        problems.append(OVERRIDDEN_LAUNCH_VERDICT.format(backend=cls.__name__))
    if _declared(cls, "label") is _UNDECLARED:
        problems.append(f"{cls.__name__} declares no label, so its operator-facing messages cannot name it")

    runtimes = _declared(cls, "runtimes")
    if runtimes is _UNDECLARED or not runtimes:
        problems.append(f"{cls.__name__} declares no runtimes, so no launch subcommand resolves")
        return problems

    for subcommand, runtime_cls in runtimes.items():
        declared_name = _declared(runtime_cls, "name")
        if declared_name is not _UNDECLARED and declared_name != subcommand:
            problems.append(
                f"{cls.__name__} maps subcommand {subcommand!r} to {runtime_cls.__name__}, which names itself "
                f"{declared_name!r}"
            )
        problems.extend(check_runtime_declarations(runtime_cls))
        problems.extend(_forbidden_signal_calls(runtime_cls))

    if any(runtime_cls.consumes_work for runtime_cls in runtimes.values()) and CONSUMING_RUNTIME not in runtimes:
        problems.append(
            f"{cls.__name__} names its consuming runtime something other than {CONSUMING_RUNTIME!r}: the host "
            "sniffs argv for that name before this module is imported, so the resolved manifest is never "
            "exported into this process"
        )

    return problems


def _overrides(cls: type[BackendRuntime], member: str) -> bool:
    """Whether ``cls`` binds ``member`` rather than inheriting the contract's
    raising default."""
    return getattr(cls, member) is not getattr(BackendRuntime, member)


def _forbidden_signal_calls(runtime_cls: type[BackendRuntime]) -> list[str]:
    """Signal installs found in ``runtime_cls``'s class body or its module.

    A vendor binding that installs a handler destroys the composed chain the host
    depends on for its teardown, and the damage is invisible until a real signal
    arrives — so it is caught by reading the source rather than by running it.

    The MODULE is read too, not just the class body: a ``request_drain`` that
    delegates to a module-level helper installs the handler just as effectively,
    and only the wider read sees it.
    """
    scopes: list[tuple[str, str]] = []
    try:
        scopes.append(("class body", inspect.getsource(runtime_cls)))
        module = inspect.getmodule(runtime_cls)
        if module is not None:
            scopes.append(("module", inspect.getsource(module)))
    except OSError:
        # One handler for both reads: a runtime the certifier cannot read is not
        # a runtime it can certify, wherever the read stopped.
        return [f"{runtime_cls.__name__} has no readable source, so its signal usage cannot be certified"]
    for scope, source in scopes:
        if _FORBIDDEN_SIGNAL_CALLS.search(source):
            return [
                f"{runtime_cls.__name__} installs its own signal handler ({scope}): the host owns the process's "
                "signal disposition and composes every subscriber, so a runtime requests a drain instead"
            ]
    return []
