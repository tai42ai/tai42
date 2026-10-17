"""Repo-root conftest: whole-repo session setup loaded before any collection.

Only in effect for a whole-repo run (rootdir here); scoped member runs anchor
their own pyproject as rootdir and never load this file.

A whole-repo run imports every workspace member's suite into one process. Each
member suite is written for its own scoped session, so process-global state a
member sets up (at conftest or test-module import, or inside a test) must be
seen by that member alone. :class:`_MemberPartitions` keeps one copy of that
state per member and swaps it in whenever code belonging to a member runs:
while one of its conftests is imported (:class:`_ConftestImportScope`), while
its nodes are collected (which imports its test modules) and while its tests
are set up, run and torn down. Each member therefore sees the state its own
scoped session would build, and never another member's.
"""

from __future__ import annotations

import atexit
import importlib.abc
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# ``prometheus_client`` freezes its value backend (multiprocess mmap vs in-process
# mutex) the first time it is imported, choosing mmap only when
# ``PROMETHEUS_MULTIPROC_DIR`` is already set. The skeleton writer paths assert the
# mmap backend. This rootdir conftest is imported before any test module, so point
# the var at a fresh dir here to freeze mmap session-wide.
#
# Tests write real counters into that dir and the monitoring health watch sums
# every per-pid file in it and keeps its alert state there, so each test process
# owns its own dir: the dir name carries the owning pid, and a value inherited
# from another process (the pytest-xdist controller, a shell export, a parent
# test run) is replaced, never shared. The skeleton suite's conftest applies the
# same rule, so within one process the first conftest's dir stands.
if not os.path.basename(os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")).startswith(
    _own_prefix := f"tai42_prometheus_{os.getpid()}_"
):
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix=_own_prefix)
    atexit.register(shutil.rmtree, os.environ["PROMETHEUS_MULTIPROC_DIR"], ignore_errors=True)

_REPO_ROOT = Path(__file__).resolve().parent


def _workspace_members(root: Path) -> tuple[Path, ...]:
    """The workspace member directories, read from ``[tool.uv.workspace]``."""
    workspace = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]
    excluded = {(root / pattern).resolve() for pattern in workspace.get("exclude", [])}
    members = {
        path.resolve()
        for pattern in workspace["members"]
        for path in root.glob(pattern)
        if (path / "pyproject.toml").is_file() and path.resolve() not in excluded
    }
    return tuple(sorted(members))


def _member_pyproject(member: Path) -> dict[str, Any]:
    return tomllib.loads((member / "pyproject.toml").read_text())


@dataclass(frozen=True)
class _GlobalSlot:
    """One piece of process-global state a member suite sets for itself.

    ``read`` returns the current value; ``write`` installs one. ``initial`` is the
    value a fresh scoped session starts from.
    """

    name: str
    read: Callable[[], Any]
    write: Callable[[Any], None]
    initial: Any


def _app_binding_slot() -> _GlobalSlot | None:
    """The ``tai42_app`` binding: member conftests and test modules bind their own app.

    ``None`` when the contract is not installed (the root-only suites, ``tests/``
    and ``scripts/tests/``, run without the workspace packages); then no suite can
    bind an app and there is no binding to partition.
    """
    if importlib.util.find_spec("tai42_contract") is None:
        return None
    from tai42_contract.app.handle import tai42_app

    def read() -> Any:
        return object.__getattribute__(tai42_app, "_impl")

    def write(impl: Any) -> None:
        tai42_app.bind(impl)

    return _GlobalSlot(name="tai42_app binding", read=read, write=write, initial=None)


_LoggerConfig = tuple[int, tuple[Any, ...], tuple[logging.Handler, ...], bool, bool]

# What ``logging.getLogger(name)`` hands out for a name nothing has configured.
_UNCONFIGURED_LOGGER: _LoggerConfig = (logging.NOTSET, (), (), True, False)


def _logger_config(logger: logging.Logger) -> _LoggerConfig:
    return (logger.level, tuple(logger.filters), tuple(logger.handlers), logger.propagate, logger.disabled)


def _named_loggers_slot() -> _GlobalSlot:
    """The configuration of every named logger: level, filters, handlers, propagation, disabled flag.

    Members configure named loggers at import (a library sets a chatty logger's level and gives it
    a handler of its own) and inside tests (an application's logging setup fits filters onto the
    loggers it owns). The value maps each configured logger's name to its configuration; a logger
    absent from it is installed unconfigured. The root logger is not part of it: pytest's logging
    plugin installs its capture handlers on the root around every test phase, and a member's tests
    restore what they change on it themselves.
    """
    manager = logging.Logger.manager

    def loggers() -> list[tuple[str, logging.Logger]]:
        return [
            (name, logger) for name, logger in list(manager.loggerDict.items()) if isinstance(logger, logging.Logger)
        ]

    def read() -> dict[str, _LoggerConfig]:
        configs = {name: _logger_config(logger) for name, logger in loggers()}
        return {name: config for name, config in configs.items() if config != _UNCONFIGURED_LOGGER}

    def write(configs: dict[str, _LoggerConfig]) -> None:
        for name, logger in loggers():
            wanted = configs.get(name, _UNCONFIGURED_LOGGER)
            if _logger_config(logger) == wanted:
                continue
            level, filters, handlers, propagate, disabled = wanted
            if logger.level != level:
                logger.setLevel(level)
            logger.filters[:] = filters
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.disabled = disabled

    return _GlobalSlot(name="named logger configuration", read=read, write=write, initial=read())


class _MemberPartitions:
    """Per-member copies of process-global state.

    The repo-level suites (anything outside a workspace member) share one
    partition, keyed ``None``. Entering a member saves the live values into the
    partition being left and installs the entered member's saved values; a member
    entered for the first time starts from each slot's ``initial`` value.
    """

    def __init__(self, members: tuple[Path, ...], slots: tuple[_GlobalSlot, ...]) -> None:
        self._members = members
        self._slots = slots
        self._active: Path | None = None
        self._saved: dict[Path | None, tuple[Any, ...]] = {}

    @property
    def members(self) -> tuple[Path, ...]:
        return self._members

    def member_of(self, path: Path) -> Path | None:
        resolved = path.resolve()
        for member in self._members:
            if resolved == member or member in resolved.parents:
                return member
        return None

    def _switch(self, target: Path | None) -> None:
        if target == self._active:
            return
        self._saved[self._active] = tuple(slot.read() for slot in self._slots)
        restored = self._saved.get(target, tuple(slot.initial for slot in self._slots))
        for slot, value in zip(self._slots, restored, strict=True):
            slot.write(value)
        self._active = target

    def enter(self, path: Path) -> None:
        """Make the partition of the member owning ``path`` the live one, and leave it live.

        The live partition changes only when code of another partition comes up next, so
        consecutive nodes of one member swap nothing.
        """
        self._switch(self.member_of(path))

    @contextmanager
    def scope(self, path: Path) -> Iterator[None]:
        """Run the block in the partition of the member owning ``path``, then go back to the one live before."""
        previous = self._active
        self._switch(self.member_of(path))
        try:
            yield
        finally:
            self._switch(previous)


_partitions = _MemberPartitions(
    _workspace_members(_REPO_ROOT),
    tuple(slot for slot in (_app_binding_slot(), _named_loggers_slot()) if slot is not None),
)


class _ScopedLoader(importlib.abc.Loader):
    """Delegates to ``inner`` and executes the module inside its member's partition."""

    def __init__(self, inner: importlib.abc.Loader, path: Path) -> None:
        self._inner = inner
        self._path = path

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return self._inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        with _partitions.scope(self._path):
            self._inner.exec_module(module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ConftestImportScope(importlib.abc.MetaPathFinder):
    """Runs every conftest import inside the partition of the member that owns it.

    pytest imports the conftests on the command-line paths at startup and the rest
    whenever collection first reaches their directory, in both cases outside the
    collection hooks below. This finder sits first on ``sys.meta_path``, lets the
    remaining finders resolve a ``*.conftest`` module as usual and wraps the
    resulting loader so the module body (its import-time binds and registrations)
    executes in its member's partition.
    """

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname.rpartition(".")[2] != "conftest":
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.origin is None or spec.loader is None:
            return spec
        spec.loader = _ScopedLoader(spec.loader, Path(spec.origin))
        return spec


_conftest_import_scope = _ConftestImportScope()
sys.meta_path.insert(0, _conftest_import_scope)


def pytest_configure(config: pytest.Config) -> None:
    """Register every member's declared markers.

    A whole-repo session reads its ini options from the repo root, never from a
    member's pyproject, so the markers each member declares for its own scoped
    session are registered here from those same declarations.
    """
    registered: set[str] = set()
    for member in _partitions.members:
        options = _member_pyproject(member).get("tool", {}).get("pytest", {}).get("ini_options", {})
        for line in options.get("markers", []):
            if line not in registered:
                registered.add(line)
                config.addinivalue_line("markers", line)


def pytest_unconfigure() -> None:
    """Take the conftest import scope back off ``sys.meta_path``."""
    sys.meta_path.remove(_conftest_import_scope)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip, with the reason, each test that needs its member's package-scoped install.

    A ``scoped_install`` test judges the environment its member's own
    ``uv sync --package`` builds (an import-graph walk compares every module the
    package pulls in against that package's dependency closure). A whole-repo
    session runs in the venv that holds every workspace member, so the verdict
    such a test would give here is about that venv and not about the package.
    """
    for item in items:
        if item.get_closest_marker("scoped_install") is None:
            continue
        member = _partitions.member_of(item.path)
        if member is None:
            raise pytest.UsageError(f"{item.nodeid}: scoped_install marks a test outside every workspace member")
        package = _member_pyproject(member)["project"]["name"]
        item.add_marker(
            pytest.mark.skip(
                reason=(
                    f"needs the package-scoped install of {package} "
                    f"(uv sync --locked --package {package}, then pytest in {member.relative_to(_REPO_ROOT)}); "
                    "a whole-repo session runs in the venv holding every workspace member"
                )
            )
        )


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Name every test this whole-repo session skipped for needing a package-scoped install."""
    skipped = [
        report
        for report in terminalreporter.stats.get("skipped", [])
        if "scoped_install" in getattr(report, "keywords", {})
    ]
    if not skipped:
        return
    terminalreporter.section("needs a package-scoped install, skipped in this whole-repo session", sep="=")
    for report in skipped:
        _, _, reason = report.longrepr
        terminalreporter.line(f"SKIPPED {report.nodeid}: {reason.removeprefix('Skipped: ')}")


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Iterator[Any]:
    """Collect a node, importing its test module, in its member's partition."""
    _partitions.enter(collector.path)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Iterator[None]:
    """Set a test up in its member's partition."""
    _partitions.enter(item.path)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """Run a test in its member's partition."""
    _partitions.enter(item.path)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    """Tear a test down in its member's partition."""
    _partitions.enter(item.path)
    return (yield)
